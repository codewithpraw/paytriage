"""
Paytriage - AI Payment Recovery Agent
=====================================

Reads a batch of failed recurring payments, decides a recovery strategy for
each one, runs a bounded recovery sequence, and reports how much revenue was
recovered.

IMPORTANT - WHAT IS REAL AND WHAT IS SIMULATED
----------------------------------------------
Real:      the decision layer (a model decides each case), the escalation
           ladder, the stopping rules, the guardrails, and the audit trail.
           These are the product.
Simulated: whether an individual retry attempt succeeds. There is no live
           payment gateway here, so outcomes are drawn from per-reason
           success rates defined in RECOVERY_ODDS below. Those rates are
           stated assumptions, not measurements.

HOW DECISIONS ARE MADE
----------------------
Each failed payment is sent to a model, which returns a structured verdict:
pursue or skip, which strategy, why, and how confident it is. Two things
constrain that verdict so the model cannot do anything unsafe:

  - Hard guardrails run BEFORE the model (see PolicyEngine.check_guardrails).
    Fraud-signature and not-worth-pursuing cases are refused in code and
    never reach the model. It cannot overrule them.
  - MAX_ATTEMPTS caps the ladder regardless of what strategy comes back.

If the model is unreachable or returns something unusable, that payment falls
back to the deterministic rule engine rather than failing the run. Every
record in the audit trail carries `decided_by` (the provider name, "guardrail"
or "fallback_rules") so the split is always visible. Run --no-ai for
rules-only.

The attempt simulation is seeded (--seed, default 42), so outcome randomness
is reproducible. Note that model decisions are not themselves deterministic,
so re-running can shift results slightly even at a fixed seed; --no-ai is
fully deterministic.

Usage:
    python agent.py
    python agent.py --input sample_payments.json --output recovery_results.json
    python agent.py --seed 7
    python agent.py --no-ai          # deterministic rules only, no API calls

Requires Python 3.9+. Install deps with `pip install -r requirements.txt`.
Default provider is Google Gemini (free API tier): set GEMINI_API_KEY, get one
at https://aistudio.google.com/apikey. Or run --no-ai with no key at all.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Policy constants
#
# Every number below is a tunable assumption. They are gathered here rather
# than scattered through the code so that a reviewer can see the whole policy
# in one screen and argue with it.
# ---------------------------------------------------------------------------

# Hard ceiling on retry attempts per payment. This is the anti-spam guarantee:
# no customer is contacted or charged more than this many times, ever.
MAX_ATTEMPTS = 3

# A customer must have at least this many prior successful payments before we
# will spend a recovery attempt on them. Brand-new customers are a retention
# problem, not a recovery problem.
MIN_HISTORY_FOR_RECOVERY = 3

# Below this lifetime value, recovery effort costs more than it returns.
MIN_CLV_FOR_RECOVERY = 1500

# Bank declines from thin-history customers are the classic fraud signature.
# We refuse to retry them at all, regardless of amount.
BANK_DECLINE_MIN_HISTORY = 5

# Probability that a single attempt succeeds, by failure reason.
# Rationale for each is in architecture.md.
RECOVERY_ODDS = {
    "card_expired": 0.72,       # customer updates card when prompted
    "insufficient_funds": 0.45,  # depends on payday timing
    "network_error": 0.85,       # usually transient, resolves on its own
    "bank_decline": 0.18,        # often a persistent block, not a glitch
}

# Segment multipliers applied to the base odds above. A loyal customer is more
# likely to act on a recovery prompt than a lapsing one.
SEGMENT_MULTIPLIER = {
    "loyal": 1.15,
    "at_risk": 0.75,
    "new": 0.55,
}

# Unknown failure reasons fall back to this. Conservative on purpose.
UNKNOWN_REASON_ODDS = 0.20

# How well each strategy actually addresses each failure reason, as a
# multiplier on the base odds above.
#
# Without this, the simulator scores *how many* attempts were made and ignores
# *which* strategy was chosen - so "notify the customer, then retry" and
# "silently retry" score identically, and a decision layer that reasons well
# about strategy shows no advantage. The base rates in RECOVERY_ODDS assume the
# appropriate strategy was used; these multipliers price the mismatch.
#
# The sharpest case is card_expired: an expired card cannot clear until new
# details exist, so retrying it silently is close to hopeless, while the 0.72
# base rate ("customer updates card when prompted") only applies if you
# actually prompted them.
STRATEGY_FIT = {
    "card_expired": {
        "notify_then_retry": 1.00,   # the customer supplies new details
        "immediate_retry": 0.25,     # nothing has changed; see updater bonus below
        "customer_contact": 0.85,    # a human gets them to update, more slowly
    },
    "insufficient_funds": {
        "notify_then_retry": 1.00,   # prompt them to top up, spaced over days
        "immediate_retry": 0.45,     # the account is still empty right now
        "customer_contact": 0.70,
    },
    "network_error": {
        "immediate_retry": 1.00,     # gateway-side fault, just try again
        "notify_then_retry": 0.90,   # works, but the notification is pure noise
        "customer_contact": 0.60,    # wasteful: a human has nothing to fix
    },
    "bank_decline": {
        "customer_contact": 1.00,    # only a human can find out what the block is
        "notify_then_retry": 0.35,
        "immediate_retry": 0.20,     # a retry cannot clear an issuer block
    },
}
DEFAULT_STRATEGY_FIT = 0.60

# Card networks run account-updater services that silently refresh stored card
# details. For a long-tenured customer, an immediate retry on an expired card
# can therefore clear without ever bothering them - the best possible outcome.
# Both the rule engine and the model reason about this case explicitly, so the
# simulator has to model it or that reasoning scores as noise.
ACCOUNT_UPDATER_MIN_HISTORY = 15
ACCOUNT_UPDATER_BONUS = 2.4

# Strategy definitions: how many attempts the ladder allows, and how many days
# to wait before each attempt. len(delays) is the attempt budget.
STRATEGIES = {
    # Transient or trivially-fixable failures. Hit it now, once more tomorrow.
    "immediate_retry": {"delays_days": [0, 1, 3]},
    # Customer has to do something (top up, update card). Tell them, then wait.
    "notify_then_retry": {"delays_days": [0, 2, 5]},
    # Something is wrong that a retry cannot fix. A human opens a ticket.
    "customer_contact": {"delays_days": [1]},
    # Not worth pursuing. Zero attempts, zero customer contact.
    "skip": {"delays_days": []},
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("payment_id", "amount", "failure_reason")


@dataclass
class Payment:
    """One failed payment, normalised from the input JSON."""

    payment_id: str
    amount: int
    failure_reason: str
    merchant_id: str = "unknown"
    customer_id: str = "unknown"
    currency: str = "INR"
    customer_history: str = "new"
    subscription_type: str = "monthly"
    previous_successful_payments: int = 0
    days_since_last_payment: int = 0
    customer_lifetime_value: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Payment":
        """Build a Payment, raising ValueError on anything unusable."""
        missing = [f for f in REQUIRED_FIELDS if raw.get(f) in (None, "")]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")

        try:
            amount = int(raw["amount"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"amount is not a number: {raw.get('amount')!r}") from exc

        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount}")

        # Only keep keys the dataclass knows about; ignore extras silently so
        # that a richer upstream schema does not break us.
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in raw.items() if k in known}
        clean["amount"] = amount

        # Coerce the numeric fields we tolerate being absent or stringly-typed.
        for field_name in (
            "previous_successful_payments",
            "days_since_last_payment",
            "customer_lifetime_value",
        ):
            try:
                clean[field_name] = int(clean.get(field_name, 0) or 0)
            except (TypeError, ValueError):
                clean[field_name] = 0

        return cls(**clean)


@dataclass
class Decision:
    """The policy engine's verdict on a single payment."""

    should_recover: bool
    strategy: str
    reasoning: str
    confidence: str  # high | medium | low


@dataclass
class Attempt:
    """One entry in the escalation ladder."""

    attempt_number: int
    day_offset: int
    action: str
    outcome: str  # success | failed


@dataclass
class CaseResult:
    """Everything that happened to one payment. This is the audit record."""

    payment_id: str
    customer_id: str
    amount: int
    failure_reason: str
    customer_history: str
    previous_successful_payments: int
    customer_lifetime_value: int
    decision: str            # recover | skip
    strategy: str
    reasoning: str
    confidence: str
    decided_by: str = "fallback_rules"  # claude | guardrail | fallback_rules
    attempts: list[Attempt] = field(default_factory=list)
    outcome: str = "not_attempted"  # recovered | unrecovered | not_attempted
    recovered_amount: int = 0
    stopped_because: str = ""


# ---------------------------------------------------------------------------
# Policy engine - the decisions
# ---------------------------------------------------------------------------

class PolicyEngine:
    """Decides whether and how to pursue a failed payment.

    Two responsibilities, deliberately separated:

    `check_guardrails()` holds the refusal rules. These run BEFORE the AI layer
    and are not negotiable - a model cannot argue its way past a fraud
    signature. They are enforced in code, not in a prompt.

    `decide()` is the full deterministic policy (guardrails plus routing). It
    is the fallback path when the AI layer is unavailable, and the whole engine
    when --no-ai is set. Every branch writes the reason it fired into the audit
    trail, so any decision can be traced back to a rule rather than a mood.
    """

    def check_guardrails(self, p: Payment) -> Decision | None:
        """Return a refusal Decision if this payment must not be pursued.

        None means "no hard rule fired" - the case is safe to hand to the AI
        layer for a judgment call.
        """
        history = p.previous_successful_payments
        clv = p.customer_lifetime_value
        reason = p.failure_reason

        if reason == "bank_decline" and history < BANK_DECLINE_MIN_HISTORY:
            return Decision(
                False,
                "skip",
                f"Bank decline with only {history} prior payments. Thin history plus "
                f"an issuer block is the common fraud signature; retrying risks "
                f"chargebacks and issuer penalties. Refer to onboarding review instead.",
                "high",
            )

        if history < MIN_HISTORY_FOR_RECOVERY:
            return Decision(
                False,
                "skip",
                f"Only {history} prior successful payment(s), below the threshold of "
                f"{MIN_HISTORY_FOR_RECOVERY}. Too little signal that this customer "
                f"intends to keep paying. This is a retention problem, not a recovery one.",
                "high",
            )

        if clv < MIN_CLV_FOR_RECOVERY:
            return Decision(
                False,
                "skip",
                f"Lifetime value of {clv} is below the {MIN_CLV_FOR_RECOVERY} floor. "
                f"Expected recovery does not cover the cost of contacting the customer.",
                "high",
            )

        return None

    def decide(self, p: Payment) -> Decision:
        history = p.previous_successful_payments
        reason = p.failure_reason
        segment = p.customer_history

        refusal = self.check_guardrails(p)
        if refusal is not None:
            return refusal

        # ---- Routing rules. The customer is worth pursuing; pick the ladder. ----

        if reason == "bank_decline":
            return Decision(
                True,
                "customer_contact",
                f"Bank decline from an established customer ({history} prior payments). "
                f"A retry cannot clear an issuer block, so this needs a human to find out "
                f"whether it is a fraud hold, a lapsed mandate, or a closed account.",
                "low",
            )

        if reason == "network_error":
            return Decision(
                True,
                "immediate_retry",
                f"Network error is a gateway-side fault, not a customer-side one. "
                f"Nothing for the customer to fix and no reason to notify them. "
                f"Retry immediately.",
                "high",
            )

        if reason == "card_expired":
            # The customer must physically update a card, so notification comes
            # first. The exception is a long-tenured customer, where an immediate
            # retry often clears on the network's updated-card service before we
            # ever need to bother them.
            if history >= 15:
                return Decision(
                    True,
                    "immediate_retry",
                    f"Card expired for a long-tenured customer ({history} payments). "
                    f"Try the account updater path first; if it clears, the customer "
                    f"never has to be contacted at all.",
                    "high",
                )
            return Decision(
                True,
                "notify_then_retry",
                f"Card expired and the customer has to enter new details. Notify first, "
                f"then retry. Retrying without telling them just burns attempts.",
                "medium",
            )

        if reason == "insufficient_funds":
            confidence = "high" if segment == "loyal" else "low"
            return Decision(
                True,
                "notify_then_retry",
                f"Insufficient funds from a {segment} customer. Likely a timing problem "
                f"rather than an intent problem, so notify and space the retries across "
                f"a pay cycle instead of hammering the same empty account.",
                confidence,
            )

        # ---- Unknown reason. Do the cautious thing, and say so. ----
        return Decision(
            True,
            "customer_contact",
            f"Unrecognised failure reason '{reason}'. Routed to human review rather than "
            f"guessing at an automated remedy.",
            "low",
        )


# ---------------------------------------------------------------------------
# AI decision layer - Claude decides the cases the guardrails allow through
# ---------------------------------------------------------------------------

# Default models per provider. Gemini's Flash family is the one that stays on
# Google's free API tier (Pro models were removed from it in April 2026), which
# is why it is the default here: the whole project runs at zero cost.
# Flash-Lite, not Flash. The newest Flash models carry a free-tier quota as low
# as 20 requests, which a real batch exhausts immediately; Lite is both faster
# and more generously provisioned. Override with --model if you have paid quota.
GEMINI_MODEL = "gemini-3.5-flash-lite"
CLAUDE_MODEL = "claude-sonnet-5"

# Free tiers cap requests per minute aggressively, and a batch of 46 payments
# run in parallel will trip that cap. A 429 is a "wait, then ask again" signal,
# not a failure - surrendering to it would silently push work onto the fallback
# rules and make it look like the model never ran. So retry with backoff.
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BASE_DELAY_SECONDS = 2.0

# Requests per minute to allow ourselves. Backoff alone is not enough against a
# hard RPM ceiling: firing a batch at full speed and retrying the rejections
# just burns the retry budget and dumps work onto the fallback rules. Pacing
# requests to stay under the limit in the first place is what actually keeps
# decisions attributed to the model. Free-tier Flash is the constraint here;
# raise --rpm on a paid tier.
DEFAULT_REQUESTS_PER_MINUTE = 10

# How many payments to decide in a single request. The free tier meters
# *requests*, not payments, so batching is what makes a 46-record run fit
# inside a 20-request daily quota: 36 eligible payments become 3 calls, not 36.
DEFAULT_BATCH_SIZE = 12

_ONE_DECISION_PROPERTIES = {
    "payment_id": {"type": "string"},
    "should_recover": {"type": "boolean"},
    "strategy": {"type": "string", "enum": sorted(STRATEGIES)},
    "reasoning": {"type": "string"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}

# One decision per payment, returned as an array keyed by payment_id so each
# verdict can be matched back to the payment it belongs to. Anything missing or
# unmatched falls back to the rule engine individually.
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _ONE_DECISION_PROPERTIES,
                "required": sorted(_ONE_DECISION_PROPERTIES),
            },
        }
    },
    "required": ["decisions"],
}

SYSTEM_PROMPT = """You decide how a subscription business should pursue a failed \
recurring payment. You are the judgment layer of a recovery pipeline; hard safety rules \
have already run before you, so every case you see is one where pursuing is permitted.

Choose one strategy:
- immediate_retry: Retry the charge now, no customer contact. For failures with nothing \
for the customer to fix, or where the card network may silently resolve it (e.g. an \
account-updater refresh for a long-tenured customer's expired card).
- notify_then_retry: Tell the customer, then retry over the following days. For failures \
the customer must act on, or that depend on timing (topping up an account).
- customer_contact: Raise a support ticket for a human. For failures a retry cannot \
clear, where a person must diagnose what is actually wrong.
- skip: Do not pursue. Use sparingly - the safety rules already removed the clear-cut \
refusals - but choose it when attempts would clearly be wasted or would annoy a customer \
for no realistic gain.

Weigh: the failure reason, the customer's segment (loyal / at_risk / new), their prior \
successful payments as a loyalty signal, how overdue the payment is relative to their \
billing cycle (~30d monthly, ~90d quarterly, ~365d annual), the amount, and lifetime \
value. Prefer the least intrusive strategy that is likely to work. Do not retry a failure \
that cannot succeed unchanged. Every customer touch has a cost - spend them where they \
plausibly convert.

In `reasoning`, give one or two sentences a support lead could read: what drove the \
call, in plain language. Set `confidence` honestly - low when the signals conflict."""


def _payment_brief(p: Payment) -> str:
    cycle = {"monthly": 30, "quarterly": 90, "annual": 365}.get(p.subscription_type)
    overdue = ""
    if cycle:
        overdue = f" (billing cycle is ~{cycle}d, so this is {p.days_since_last_payment - cycle:+d}d vs cycle)"
    return (
        f"payment_id: {p.payment_id}\n"
        f"failure_reason: {p.failure_reason}\n"
        f"amount: {p.amount} {p.currency}\n"
        f"customer_segment: {p.customer_history}\n"
        f"previous_successful_payments: {p.previous_successful_payments}\n"
        f"subscription_type: {p.subscription_type}\n"
        f"days_since_last_payment: {p.days_since_last_payment}{overdue}\n"
        f"customer_lifetime_value: {p.customer_lifetime_value} {p.currency}\n"
    )


def _batch_brief(payments: list[Payment]) -> str:
    """Render several payments as one prompt, each delimited and id-tagged."""
    blocks = [f"--- PAYMENT {i} ---\n{_payment_brief(p)}" for i, p in enumerate(payments, 1)]
    return (
        f"Decide each of the following {len(payments)} failed payments independently.\n"
        f"Return one decision per payment, echoing its payment_id exactly.\n\n"
        + "\n".join(blocks)
    )


class ProviderError(Exception):
    """A transport-level failure, tagged with a short machine-readable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RateLimiter:
    """Spaces calls across threads so a batch stays under a requests/minute cap.

    Deliberately paces *before* sending rather than reacting to 429s. Against a
    hard RPM ceiling, reactive backoff loses work: the rejected calls exhaust
    their retries and fall through to the rule engine, which understates how
    much the model actually decided.
    """

    def __init__(self, requests_per_minute: float):
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        """Block until this caller's turn. Safe to call from many threads."""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            # Reserve this slot before releasing the lock, so concurrent callers
            # queue up behind each other instead of all sleeping to the same instant.
            self._next_at = max(now, self._next_at) + self.min_interval
        if wait:
            time.sleep(wait)


class GeminiProvider:
    """Google Gemini via the google-genai SDK.

    The default provider because the Flash family runs on Google's free API
    tier - no card, no billing account. Keep billing DISABLED on the API
    project: enabling it removes the free tier entirely for that project and
    every call bills from the first token.
    """

    name = "gemini"

    def __init__(self, model: str = GEMINI_MODEL):
        from google import genai  # imported lazily so --no-ai needs no SDK

        self.client = genai.Client()
        self.model = model

    def complete(self, system: str, user: str) -> str:
        try:
            interaction = self.client.interactions.create(
                model=self.model,
                system_instruction=system,
                input=user,
                # Do NOT also pass a top-level response_mime_type. The API
                # rejects that combination ("responseFormat must be set when
                # responseMimeType is set"); the mime type belongs nested here.
                # `schema_` is the SDK's field name - it serialises to "schema".
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema_": DECISION_SCHEMA,
                },
            )
        except Exception as exc:
            # The interactions API raises from google.genai._gaos.lib.compat_errors,
            # a different hierarchy than google.genai.errors. Rather than import a
            # private module that may be reorganised, classify by HTTP status,
            # which every status-bearing exception in that hierarchy exposes.
            status = getattr(exc, "status_code", None)
            if status is None:
                raise ProviderError(f"transport_{type(exc).__name__}") from exc
            if status == 429:
                raise ProviderError("rate_limited_429") from exc
            if status >= 500:
                raise ProviderError(f"server_error_{status}") from exc
            raise ProviderError(f"client_error_{status}") from exc

        text = interaction.output_text
        if not text:
            raise ProviderError("empty_output")
        return text


class ClaudeProvider:
    """Anthropic Claude. Requires purchased API credits - there is no free tier."""

    name = "claude"

    def __init__(self, model: str = CLAUDE_MODEL):
        import anthropic  # imported lazily; Gemini-only runs don't need this SDK

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, user: str) -> str:
        anthropic = self._anthropic
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            )
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"api_error_{exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("connection_error") from exc

        if response.stop_reason == "refusal":
            raise ProviderError("model_refused")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise ProviderError(f"no_text_output_{response.stop_reason}")
        return text


def build_provider(name: str) -> tuple[Any | None, str | None]:
    """Construct a provider. Returns (provider, error_message).

    Credential problems surface here, once, rather than as one identical
    failure per payment.
    """
    try:
        if name == "gemini":
            return GeminiProvider(), None
        if name == "claude":
            return ClaudeProvider(), None
        return None, f"unknown provider {name!r}"
    except ImportError as exc:
        return None, f"SDK not installed ({exc}). Try: pip install -r requirements.txt"
    except Exception as exc:
        # Both SDKs raise client-side when no credential source resolves.
        return None, f"could not initialise {name} ({type(exc).__name__}: {exc})"


def _strip_code_fence(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite a schema. Unwrap it."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[-1]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


class AIDecider:
    """Asks a model to decide a case, with the rule engine as the safety net.

    The provider handles transport; this class owns validation and fallback.
    Never raises for a single payment: any failure - transport, refusal,
    malformed output, an out-of-range strategy - is recorded and the payment
    falls back to the deterministic engine, so one bad response cannot take
    down a batch.
    """

    def __init__(
        self,
        provider: Any,
        fallback: PolicyEngine,
        requests_per_minute: float = DEFAULT_REQUESTS_PER_MINUTE,
    ):
        self.provider = provider
        self.fallback = fallback
        self.failures: list[dict[str, str]] = []
        self.retries = 0
        self.limiter = RateLimiter(requests_per_minute)

    @property
    def model(self) -> str:
        return self.provider.model

    def preflight(self) -> str | None:
        """One cheap call to confirm the provider actually answers.

        Deliberately sends the same response_format the real calls use, so a
        malformed request shape is caught here rather than 46 times over.
        """
        try:
            self.provider.complete("Reply with the single word: ok", "ping")
        except ProviderError as exc:
            code = exc.code
            if code.endswith(("401", "403")):
                return f"credentials rejected ({code}) - check the API key"
            if code.endswith("404"):
                return f"model {self.provider.model!r} not available to this key"
            if code.endswith("400"):
                # A malformed request, not a credentials problem. Saying so
                # plainly matters: mislabelling it sends debugging the wrong way.
                return f"request rejected as malformed ({code}) - this is a bug in the request shape, not your key"
            return None  # rate limit or transient; per-payment path handles it
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def _note_failure(self, p: Payment, error: str) -> tuple[Decision, str]:
        self.failures.append({"payment_id": p.payment_id, "error": error})
        return self.fallback.decide(p), "fallback_rules"

    @staticmethod
    def _is_retryable(code: str) -> bool:
        """429 (rate limit) and 5xx are worth waiting out; 4xx generally is not."""
        lowered = code.lower()
        return "429" in lowered or "resource_exhausted" in lowered or "server_error" in lowered

    def _complete_with_retry(self, system: str, user: str) -> str:
        """Call the provider, backing off on rate limits instead of giving up.

        Free API tiers cap requests per minute, so a parallel batch reliably
        trips 429s. Without this, those payments would quietly fall through to
        the rule engine and the run would understate how much the model did.
        """
        delay = RATE_LIMIT_BASE_DELAY_SECONDS
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                self.limiter.acquire()
                return self.provider.complete(system, user)
            except ProviderError as exc:
                if attempt == RATE_LIMIT_RETRIES or not self._is_retryable(exc.code):
                    raise
                self.retries += 1
                # Jitter avoids a thundering herd when the whole pool is throttled.
                # Uses the module RNG, not the seeded simulator RNG, so this
                # cannot perturb outcome reproducibility.
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
        raise ProviderError("retry_loop_exhausted")  # unreachable

    def _parse_one(self, raw: Any) -> Decision | str:
        """Validate a single decision object. Returns a Decision or an error code."""
        if not isinstance(raw, dict):
            return "entry_not_an_object"

        strategy = raw.get("strategy")
        if strategy not in STRATEGIES:
            return f"unknown_strategy_{strategy!r}"
        if not isinstance(raw.get("should_recover"), bool):
            return "missing_should_recover"

        confidence = raw.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        # A "pursue" verdict paired with the no-op strategy is contradictory;
        # treat it as the skip it actually describes.
        should_recover = raw["should_recover"] and strategy != "skip"

        return Decision(
            should_recover=should_recover,
            strategy=strategy,
            reasoning=str(raw.get("reasoning", "")).strip() or "(no reasoning returned)",
            confidence=confidence,
        )

    def decide_batch(self, batch: list[Payment]) -> dict[str, tuple[Decision, str]]:
        """Decide a batch of payments in one request.

        Returns a verdict for every payment in `batch` without exception: any
        payment the model omits, mislabels, or returns badly is filled in from
        the rule engine and recorded in `failures`. Batching exists because the
        free tier meters requests, not payments - see DEFAULT_BATCH_SIZE.
        """
        out: dict[str, tuple[Decision, str]] = {}

        def fail_all(code: str) -> dict[str, tuple[Decision, str]]:
            return {p.payment_id: self._note_failure(p, code) for p in batch}

        try:
            text = self._complete_with_retry(SYSTEM_PROMPT, _batch_brief(batch))
        except ProviderError as exc:
            return fail_all(exc.code)
        except Exception as exc:  # never let one batch break the run
            return fail_all(f"unexpected_{type(exc).__name__}")

        try:
            raw = json.loads(_strip_code_fence(text))
        except json.JSONDecodeError:
            return fail_all("invalid_json")

        if not isinstance(raw, dict) or not isinstance(raw.get("decisions"), list):
            return fail_all("output_missing_decisions_array")

        # Index the returned decisions by payment_id. A model that echoes an id
        # we never sent is ignored rather than trusted.
        wanted = {p.payment_id: p for p in batch}
        returned: dict[str, Any] = {}
        for entry in raw["decisions"]:
            if isinstance(entry, dict):
                pid = entry.get("payment_id")
                if isinstance(pid, str) and pid in wanted:
                    returned.setdefault(pid, entry)

        for p in batch:
            entry = returned.get(p.payment_id)
            if entry is None:
                out[p.payment_id] = self._note_failure(p, "missing_from_batch_response")
                continue
            parsed = self._parse_one(entry)
            if isinstance(parsed, str):
                out[p.payment_id] = self._note_failure(p, parsed)
            else:
                out[p.payment_id] = (parsed, self.provider.name)

        return out


def decide_all(
    payments: list[Payment],
    engine: PolicyEngine,
    decider: AIDecider | None,
    concurrency: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[tuple[Decision, str]]:
    """Decide every payment: guardrails first, then batched AI calls in parallel."""
    verdicts: list[tuple[Decision, str] | None] = [None] * len(payments)
    to_ask: list[int] = []

    for i, p in enumerate(payments):
        refusal = engine.check_guardrails(p)
        if refusal is not None:
            verdicts[i] = (refusal, "guardrail")
        elif decider is None:
            verdicts[i] = (engine.decide(p), "fallback_rules")
        else:
            to_ask.append(i)

    if to_ask:
        size = max(1, batch_size)
        batches = [to_ask[i:i + size] for i in range(0, len(to_ask), size)]
        index_of = {payments[i].payment_id: i for i in to_ask}

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(decider.decide_batch, [payments[i] for i in idxs]): idxs
                for idxs in batches
            }
            for future in as_completed(futures):
                idxs = futures[future]
                try:
                    for pid, verdict in future.result().items():
                        verdicts[index_of[pid]] = verdict
                except Exception as exc:
                    for i in idxs:
                        decider.failures.append(
                            {"payment_id": payments[i].payment_id, "error": f"unexpected_{type(exc).__name__}"}
                        )
                        verdicts[i] = (engine.decide(payments[i]), "fallback_rules")

    # Guarantee no payment is left undecided, whatever happened above.
    for i, p in enumerate(payments):
        if verdicts[i] is None:
            verdicts[i] = (engine.decide(p), "fallback_rules")

    return [v for v in verdicts if v is not None]


# ---------------------------------------------------------------------------
# Recovery simulator - the outcomes
# ---------------------------------------------------------------------------

class RecoverySimulator:
    """Runs the escalation ladder for a decided payment.

    Every attempt outcome here is a coin flip weighted by RECOVERY_ODDS. It is
    a model of a payment gateway, not a payment gateway. Swapping this class for
    real Razorpay test-mode calls is the intended next step and would not
    require touching PolicyEngine.
    """

    def __init__(self, seed: int):
        # Each payment draws from its own stream, derived from the run seed and
        # the payment id - NOT from one shared sequence. With a shared RNG, a
        # decision that changes one payment's attempt count shifts every later
        # payment's draws too, so --compare would attribute unrelated luck to
        # the decision layer. Per-payment streams keep the A/B honest: a payment's
        # outcome depends only on its own decision.
        self.seed = seed

    def _rng_for(self, p: Payment) -> random.Random:
        return random.Random(f"{self.seed}:{p.payment_id}")

    def _odds(self, p: Payment, attempt_number: int, strategy: str) -> float:
        base = RECOVERY_ODDS.get(p.failure_reason, UNKNOWN_REASON_ODDS)
        base *= SEGMENT_MULTIPLIER.get(p.customer_history, 0.6)

        # Does this strategy actually address why the payment failed?
        fit = STRATEGY_FIT.get(p.failure_reason, {}).get(strategy, DEFAULT_STRATEGY_FIT)
        if (
            p.failure_reason == "card_expired"
            and strategy == "immediate_retry"
            and p.previous_successful_payments >= ACCOUNT_UPDATER_MIN_HISTORY
        ):
            # Long-tenured customer: the account updater may already have fresh
            # details on file, so this is a real shortcut rather than a wasted try.
            fit *= ACCOUNT_UPDATER_BONUS
        base *= fit

        # Each successive attempt on the same payment is less likely to work
        # than the one before. If the first two failed, the third rarely saves it.
        base *= 0.65 ** (attempt_number - 1)

        return max(0.0, min(base, 0.95))

    def run(self, p: Payment, decision: Decision, decided_by: str = "fallback_rules") -> CaseResult:
        rng = self._rng_for(p)
        result = CaseResult(
            payment_id=p.payment_id,
            customer_id=p.customer_id,
            amount=p.amount,
            failure_reason=p.failure_reason,
            customer_history=p.customer_history,
            previous_successful_payments=p.previous_successful_payments,
            customer_lifetime_value=p.customer_lifetime_value,
            decision="recover" if decision.should_recover else "skip",
            strategy=decision.strategy,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
            decided_by=decided_by,
        )

        if not decision.should_recover:
            result.outcome = "not_attempted"
            result.stopped_because = "policy_declined_recovery"
            return result

        delays = STRATEGIES[decision.strategy]["delays_days"]
        # The global ceiling wins even if a strategy asks for more.
        delays = delays[:MAX_ATTEMPTS]

        for i, day_offset in enumerate(delays, start=1):
            if decision.strategy == "customer_contact":
                action = "support ticket raised for manual investigation"
            elif decision.strategy == "notify_then_retry" and i == 1:
                action = "notification sent, then charge retried"
            else:
                action = "charge retried"

            succeeded = rng.random() < self._odds(p, i, decision.strategy)
            result.attempts.append(
                Attempt(
                    attempt_number=i,
                    day_offset=day_offset,
                    action=action,
                    outcome="success" if succeeded else "failed",
                )
            )

            if succeeded:
                result.outcome = "recovered"
                result.recovered_amount = p.amount
                result.stopped_because = "recovered_on_attempt_%d" % i
                return result

        result.outcome = "unrecovered"
        result.stopped_because = (
            f"exhausted_attempt_budget_after_{len(delays)}"
            if delays
            else "no_attempts_configured"
        )
        return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _pct(numerator: float, denominator: float) -> float:
    """Percentage, rounded to one place, safe when the denominator is zero."""
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def _empty_bucket() -> dict[str, int]:
    return {"count": 0, "amount": 0, "attempted": 0, "recovered": 0, "recovered_amount": 0}


def build_report(
    results: list[CaseResult],
    skipped_records: list[dict[str, str]],
    seed: int,
    ai_enabled: bool = False,
    ai_failures: list[dict[str, str]] | None = None,
    provider: str | None = None,
    model: str | None = None,
    rate_limit_retries: int = 0,
) -> dict[str, Any]:
    total_amount = sum(r.amount for r in results)
    attempted = [r for r in results if r.decision == "recover"]
    recovered = [r for r in results if r.outcome == "recovered"]
    recovered_amount = sum(r.recovered_amount for r in results)

    by_reason: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
    by_segment: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
    by_strategy: dict[str, dict[str, int]] = defaultdict(_empty_bucket)

    for r in results:
        for bucket in (
            by_reason[r.failure_reason],
            by_segment[r.customer_history],
            by_strategy[r.strategy],
        ):
            bucket["count"] += 1
            bucket["amount"] += r.amount
            if r.decision == "recover":
                bucket["attempted"] += 1
            if r.outcome == "recovered":
                bucket["recovered"] += 1
                bucket["recovered_amount"] += r.recovered_amount

    def finish(buckets: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
        out = {}
        for key, b in sorted(buckets.items()):
            out[key] = {
                **b,
                "recovery_rate_percent": _pct(b["recovered"], b["count"]),
                "attempt_success_rate_percent": _pct(b["recovered"], b["attempted"]),
            }
        return out

    total_attempts = sum(len(r.attempts) for r in results)

    decided_by_counts: dict[str, int] = defaultdict(int)
    for r in results:
        decided_by_counts[r.decided_by] += 1

    return {
        "metadata": {
            "project": "Paytriage",
            "track": "AI Revenue Recovery",
            "generated_on": date.today().isoformat(),
            "generated_by": "agent.py",
            "random_seed": seed,
            "outcomes_are_simulated": True,
            "simulation_note": (
                "Decision layer, escalation ladder, stopping rules, guardrails and audit "
                "trail are real. Individual attempt outcomes are drawn from the assumed "
                "per-reason success rates in RECOVERY_ODDS, not from a live payment gateway."
            ),
            "decision_layer": {
                "ai_enabled": ai_enabled,
                "provider": provider if ai_enabled else None,
                "model": model if ai_enabled else None,
                "decided_by": dict(sorted(decided_by_counts.items())),
                "ai_failures": ai_failures or [],
                "rate_limit_retries": rate_limit_retries,
                "note": (
                    "Guardrail decisions are refusals enforced in code before the model is "
                    "consulted. Cases marked fallback_rules fell back to the deterministic "
                    "engine because the AI call failed; see ai_failures."
                ),
            },
            "policy": {
                "max_attempts_per_payment": MAX_ATTEMPTS,
                "min_prior_payments_for_recovery": MIN_HISTORY_FOR_RECOVERY,
                "min_customer_lifetime_value": MIN_CLV_FOR_RECOVERY,
                "bank_decline_min_history": BANK_DECLINE_MIN_HISTORY,
                "assumed_base_odds": RECOVERY_ODDS,
                "segment_multipliers": SEGMENT_MULTIPLIER,
            },
        },
        "summary": {
            "payments_processed": len(results),
            "malformed_records_skipped": len(skipped_records),
            "amount_at_risk": total_amount,
            "recovery_attempted": len(attempted),
            "recovery_declined": len(results) - len(attempted),
            "recovered_count": len(recovered),
            "recovered_amount": recovered_amount,
            "unrecovered_amount": total_amount - recovered_amount,
            "recovery_rate_percent": _pct(len(recovered), len(results)),
            "attempt_success_rate_percent": _pct(len(recovered), len(attempted)),
            "amount_recovery_rate_percent": _pct(recovered_amount, total_amount),
            "total_customer_touches": total_attempts,
            "avg_touches_per_pursued_payment": (
                round(total_attempts / len(attempted), 2) if attempted else 0.0
            ),
        },
        "by_failure_reason": finish(by_reason),
        "by_customer_segment": finish(by_segment),
        "by_strategy": finish(by_strategy),
        "malformed_records": skipped_records,
        "audit_log": [asdict(r) for r in results],
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_payments(path: Path) -> tuple[list[Payment], list[dict[str, str]]]:
    """Load and validate payments. Returns (usable, rejected-with-reasons)."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(
            f"Input file not found: {path}\n"
            f"Expected the payments file there. Pass a different path with --input."
        )
    except OSError as exc:
        raise SystemExit(f"Could not read {path}: {exc}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")

    # Accept either {"failed_payments": [...]} or a bare [...].
    if isinstance(data, dict):
        records = data.get("failed_payments")
        if records is None:
            raise SystemExit(
                f"{path} is a JSON object but has no 'failed_payments' key. "
                f"Found keys: {', '.join(sorted(data)) or '(none)'}"
            )
    elif isinstance(data, list):
        records = data
    else:
        raise SystemExit(f"{path} must contain a list or an object, got {type(data).__name__}")

    if not isinstance(records, list):
        raise SystemExit("'failed_payments' must be a list.")

    payments: list[Payment] = []
    rejected: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            rejected.append({"index": str(index), "error": "record is not an object"})
            continue
        try:
            payment = Payment.from_dict(record)
        except ValueError as exc:
            rejected.append(
                {
                    "index": str(index),
                    "payment_id": str(record.get("payment_id", "<none>")),
                    "error": str(exc),
                }
            )
            continue

        if payment.payment_id in seen_ids:
            rejected.append(
                {
                    "index": str(index),
                    "payment_id": payment.payment_id,
                    "error": "duplicate payment_id; first occurrence kept",
                }
            )
            continue

        seen_ids.add(payment.payment_id)
        payments.append(payment)

    return payments, rejected


COMPARE_ROWS = [
    # (label, sub-label, key path, formatter, higher-is-better)
    ("Amount recovered", "of total at risk", "recovered_amount", "money", True),
    ("Amount recovery rate", "", "amount_recovery_rate_percent", "pct", True),
    ("Payments recovered", "", "recovered_count", "int", True),
    ("Pursued", "", "recovery_attempted", "int", None),
    ("Success rate when pursued", "", "attempt_success_rate_percent", "pct", True),
    ("Refused by guardrails", "enforced in code", "recovery_declined", "int", None),
    ("Customer touches", "", "total_customer_touches", "int", False),
]


def build_comparison(rules: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    """Diff two reports produced from identical input and seed.

    The only variable between them is who decided each case, so every delta
    here is attributable to the decision layer rather than to chance.
    """
    rows = []
    for label, sub, key, fmt, higher_better in COMPARE_ROWS:
        a, b = rules["summary"][key], ai["summary"][key]
        delta = round(b - a, 2)
        if higher_better is None or delta == 0:
            direction = "flat"
        else:
            improved = (delta > 0) == higher_better
            direction = "up" if improved else "down"
        rows.append(
            {
                "label": label, "sublabel": sub, "key": key, "format": fmt,
                "rules": a, "ai": b, "delta": delta, "direction": direction,
            }
        )

    # Which individual payments did the two engines decide differently?
    rules_by_id = {c["payment_id"]: c for c in rules["audit_log"]}
    divergent = []
    for c in ai["audit_log"]:
        other = rules_by_id.get(c["payment_id"])
        if other and other["strategy"] != c["strategy"]:
            divergent.append(
                {
                    "payment_id": c["payment_id"],
                    "amount": c["amount"],
                    "failure_reason": c["failure_reason"],
                    "customer_history": c["customer_history"],
                    "rules_strategy": other["strategy"],
                    "ai_strategy": c["strategy"],
                    "ai_reasoning": c["reasoning"],
                    "rules_outcome": other["outcome"],
                    "ai_outcome": c["outcome"],
                    "swing": c["recovered_amount"] - other["recovered_amount"],
                }
            )
    divergent.sort(key=lambda d: -abs(d["swing"]))

    return {
        "rows": rows,
        "divergent_decisions": divergent,
        "divergent_count": len(divergent),
        "net_swing": sum(d["swing"] for d in divergent),
    }


def print_comparison(comparison: dict[str, Any], ai_model: str) -> None:
    line = "-" * 74
    print()
    print("RULES vs AI - identical input, identical seed, only the decider changes")
    print(line)
    print(f"  {'':<32}{'rules':>12}{'ai':>14}{'delta':>14}")
    print(line)
    for r in comparison["rows"]:
        fmt = r["format"]
        def show(v: float) -> str:
            if fmt == "money":
                return f"INR {v:,.0f}"
            if fmt == "pct":
                return f"{v}%"
            return f"{v:,.0f}"
        d = r["delta"]
        sign = "+" if d > 0 else ""
        mark = {"up": "  +", "down": "  -", "flat": "   "}[r["direction"]]
        print(f"  {r['label']:<32}{show(r['rules']):>12}{show(r['ai']):>14}"
              f"{sign + show(d) if d else '--':>13}{mark}")
    print(line)
    n = comparison["divergent_count"]
    swing = comparison["net_swing"]
    print(f"  {n} payment(s) decided differently by {ai_model}, net swing INR {swing:+,}")
    if comparison["divergent_decisions"]:
        print()
        print("  Largest divergences:")
        for d in comparison["divergent_decisions"][:5]:
            print(f"    {d['payment_id']}  INR {d['amount']:>6,}  {d['failure_reason']:<19}"
                  f" {d['rules_strategy']} -> {d['ai_strategy']}  ({d['swing']:+,})")
    print()


# ---------------------------------------------------------------------------
# HTML report
#
# Self-contained: no framework, no CDN, no build step. Written as one string
# because the whole point is that a judge can double-click the output file and
# see the run - adding a template dependency to render a report would trade
# that away for nothing.
# ---------------------------------------------------------------------------

HTML_STYLE = """
:root{--ink:#16202f;--ink-soft:#3c4759;--muted:#6b7688;--stock:#f6f7f9;--card:#fff;
--rule:#dde2ea;--rule-firm:#c3cbd8;--recovered:#0f6b57;--recovered-w:#e3f0ec;
--lost:#9c3f2f;--lost-w:#f6e8e4;--guard:#8a6410;--guard-w:#f7eeda;--ai:#2b4b8f;--ai-w:#e6ebf6;
--sans:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,"SF Mono",monospace;
--serif:"Newsreader",Georgia,"Times New Roman",serif}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--ink:#e8ecf3;--ink-soft:#b3bccc;
--muted:#8996a9;--stock:#10161f;--card:#171f2b;--rule:#28323f;--rule-firm:#3a4655;
--recovered:#4cbfa2;--recovered-w:#12302a;--lost:#d98570;--lost-w:#331e18;
--guard:#d9a942;--guard-w:#33280f;--ai:#8aa8e8;--ai-w:#1a2440}}
:root[data-theme="dark"]{--ink:#e8ecf3;--ink-soft:#b3bccc;--muted:#8996a9;--stock:#10161f;
--card:#171f2b;--rule:#28323f;--rule-firm:#3a4655;--recovered:#4cbfa2;--recovered-w:#12302a;
--lost:#d98570;--lost-w:#331e18;--guard:#d9a942;--guard-w:#33280f;--ai:#8aa8e8;--ai-w:#1a2440}
*{box-sizing:border-box}
body{background:var(--stock);color:var(--ink);font-family:var(--sans);font-size:15px;
line-height:1.55;margin:0;padding:0 20px 72px;-webkit-font-smoothing:antialiased}
.sheet{max-width:1080px;margin:0 auto}
.masthead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;
gap:20px;padding:40px 0 18px;border-bottom:2px solid var(--ink)}
.wordmark{font-family:var(--serif);font-size:30px;font-weight:600;letter-spacing:-.015em;margin:0;line-height:1.1}
.wordmark span{color:var(--muted);font-weight:400}
.doctype{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
color:var(--muted);margin-top:6px}
.runmeta{display:grid;grid-template-columns:auto auto;gap:3px 20px;font-family:var(--mono);
font-size:11.5px;color:var(--muted)}
.runmeta b{color:var(--ink-soft);font-weight:500;text-align:right}
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));border-bottom:1px solid var(--rule)}
.fig{padding:26px 24px 26px 0;border-right:1px solid var(--rule)}
.fig:last-child{border-right:0}
.fig-label{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
color:var(--muted);margin-bottom:8px}
.fig-value{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:30px;
font-weight:600;letter-spacing:-.02em;line-height:1}
.fig-value.pos{color:var(--recovered)}.fig-value.neg{color:var(--lost)}
.fig-note{font-size:12.5px;color:var(--muted);margin-top:7px}
section{margin-top:44px}
h2{font-family:var(--serif);font-size:21px;font-weight:600;margin:0 0 4px;
letter-spacing:-.01em;text-wrap:balance}
.sub{color:var(--muted);font-size:13.5px;margin:0 0 18px;max-width:64ch}
.panel{border:1px solid var(--rule-firm);background:var(--card);overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
.panel th,.panel td{padding:11px 16px;text-align:right;white-space:nowrap}
.panel th:first-child,.panel td:first-child{text-align:left;white-space:normal}
.panel thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;
text-transform:uppercase;color:var(--muted);font-weight:500;
border-bottom:1px solid var(--rule-firm);background:var(--stock)}
.panel tbody td{border-bottom:1px solid var(--rule)}
.panel tbody tr:last-child td{border-bottom:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.col-ai{background:var(--ai-w);font-weight:600}
.delta{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:500}
.delta.up{color:var(--recovered)}.delta.down{color:var(--lost)}.delta.flat{color:var(--muted)}
.rowlabel{font-weight:500}
.rowlabel small{display:block;font-weight:400;color:var(--muted);font-size:12px}
tr.headline td{background:var(--recovered-w)}
tr.headline .num{font-size:15px;font-weight:600}
.reasons{display:grid;gap:14px}
.reason{display:grid;grid-template-columns:190px 1fr 152px;gap:16px;align-items:center}
.reason-name{font-family:var(--mono);font-size:12.5px}
.track{height:22px;background:var(--rule);position:relative}
.fill{height:100%;background:var(--recovered)}
.reason-fig{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:12.5px;
text-align:right;color:var(--ink-soft)}
.reason-fig b{color:var(--ink);font-weight:600}
.ledger{border:1px solid var(--rule-firm);background:var(--card);overflow-x:auto}
.ledger table{font-size:13px;min-width:920px}
.ledger th{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
color:var(--muted);font-weight:500;text-align:left;padding:11px 14px;background:var(--stock);
border-bottom:1px solid var(--rule-firm)}
.ledger td{padding:12px 14px;border-bottom:1px solid var(--rule);vertical-align:top}
.ledger tbody tr:last-child td{border-bottom:0}
.ledger .amt{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.pid{font-family:var(--mono);font-size:12px}
.why{color:var(--ink-soft);font-size:12.5px;line-height:1.45;max-width:42ch}
.chip{display:inline-block;font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;
padding:2px 7px;white-space:nowrap;border:1px solid currentColor}
.chip.ai{color:var(--ai);background:var(--ai-w)}
.chip.guard{color:var(--guard);background:var(--guard-w)}
.chip.rules{color:var(--muted);background:var(--stock)}
.chip.ok{color:var(--recovered);background:var(--recovered-w)}
.chip.no{color:var(--lost);background:var(--lost-w)}
.strategy{font-family:var(--mono);font-size:12px}
.conf{color:var(--muted);font-size:11.5px;font-family:var(--mono)}
.note{border-left:3px solid var(--guard);background:var(--guard-w);padding:14px 18px;
font-size:13.5px;color:var(--ink-soft)}
.note b{color:var(--ink)}
.note.caveat{border-left-color:var(--muted);background:transparent;border:1px solid var(--rule)}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--rule);font-family:var(--mono);
font-size:11px;color:var(--muted);display:flex;flex-wrap:wrap;gap:8px 24px;justify-content:space-between}
@media(max-width:720px){.reason{grid-template-columns:1fr;gap:5px}.reason-fig{text-align:left}
.fig{border-right:0;border-bottom:1px solid var(--rule);padding-right:0}}
"""


def _esc(text: Any) -> str:
    """Escape untrusted text. Model-written reasoning lands in this HTML."""
    return html.escape(str(text), quote=True)


def _money(v: float) -> str:
    return f"&#8377;{v:,.0f}"


def _fmt(value: float, fmt: str) -> str:
    if fmt == "money":
        return _money(value)
    if fmt == "pct":
        return f"{value}%"
    return f"{value:,.0f}"


def _render_compare(comparison: dict[str, Any]) -> str:
    rows = []
    for i, r in enumerate(comparison["rows"]):
        sub = f"<small>{_esc(r['sublabel'])}</small>" if r["sublabel"] else ""
        d = r["delta"]
        if d == 0:
            delta_txt = "&mdash;"
        else:
            delta_txt = ("+" if d > 0 else "&minus;") + _fmt(abs(d), r["format"])
        rows.append(
            f'<tr{" class=\"headline\"" if i == 0 else ""}>'
            f'<td class="rowlabel">{_esc(r["label"])}{sub}</td>'
            f'<td class="num">{_fmt(r["rules"], r["format"])}</td>'
            f'<td class="num col-ai">{_fmt(r["ai"], r["format"])}</td>'
            f'<td class="delta {r["direction"]}">{delta_txt}</td></tr>'
        )

    divergent = ""
    if comparison["divergent_decisions"]:
        items = []
        for d in comparison["divergent_decisions"][:6]:
            swing = d["swing"]
            cls = "up" if swing > 0 else ("down" if swing < 0 else "flat")
            sign = "+" if swing > 0 else ("&minus;" if swing < 0 else "")
            items.append(
                f'<tr><td class="pid">{_esc(d["payment_id"])}</td>'
                f'<td class="num">{_money(d["amount"])}</td>'
                f'<td><span class="strategy">{_esc(d["failure_reason"])}</span></td>'
                f'<td><span class="strategy">{_esc(d["rules_strategy"])}</span> &rarr; '
                f'<span class="strategy">{_esc(d["ai_strategy"])}</span></td>'
                f'<td class="delta {cls}">{sign}{_money(abs(swing))}</td></tr>'
            )
        divergent = (
            '<p class="sub" style="margin-top:26px">'
            f'<b>{comparison["divergent_count"]} payment(s)</b> were decided differently, '
            f'a net swing of {_money(abs(comparison["net_swing"]))}. '
            "Every other case, both engines agreed on.</p>"
            '<div class="panel"><table><thead><tr>'
            "<th>Payment</th><th>Amount</th><th>Failure</th>"
            "<th>Rules &rarr; AI</th><th>Swing</th>"
            "</tr></thead><tbody>" + "".join(items) + "</tbody></table></div>"
        )

    return (
        "<section><h2>Rule engine vs. AI decisions</h2>"
        '<p class="sub">Identical input, identical simulator seed. The only variable is who '
        "decided each case. Guardrail refusals are enforced in code and are the same in both "
        "columns.</p>"
        '<div class="panel"><table><thead><tr><th>Measure</th><th>Rules only</th>'
        "<th>With AI</th><th>Delta</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>" + divergent + "</section>"
    )


def render_html(report: dict[str, Any], comparison: dict[str, Any] | None = None) -> str:
    meta, s = report["metadata"], report["summary"]
    layer = meta["decision_layer"]
    engine_name = layer["model"] if layer["ai_enabled"] else "deterministic rules"

    runmeta = [
        ("Records", s["payments_processed"]),
        ("Decision engine", engine_name),
        ("Fallbacks", layer["decided_by"].get("fallback_rules", 0)),
        ("Guardrail refusals", layer["decided_by"].get("guardrail", 0)),
        ("Generated", meta["generated_on"]),
        ("Seed", meta["random_seed"]),
    ]
    meta_html = "".join(
        f"<span>{_esc(k)}</span><b>{_esc(v)}</b>" for k, v in runmeta
    )

    merchants = len({c.get("merchant_id") for c in report["audit_log"] if c.get("merchant_id")})
    figs = [
        ("At risk", _money(s["amount_at_risk"]), "",
         f'{s["payments_processed"]} failed payments'
         + (f" across {merchants} merchants" if merchants else "")),
        ("Recovered", _money(s["recovered_amount"]), "pos",
         f'{s["recovered_count"]} payments &middot; {s["amount_recovery_rate_percent"]}% of value'),
        ("Written off", _money(s["unrecovered_amount"]), "neg",
         f'{s["payments_processed"] - s["recovered_count"]} unrecovered or refused'),
        ("Customer touches", f'{s["total_customer_touches"]:,}', "",
         f'{s["avg_touches_per_pursued_payment"]} avg &middot; ceiling {MAX_ATTEMPTS}'),
    ]
    figs_html = "".join(
        f'<div class="fig"><div class="fig-label">{_esc(l)}</div>'
        f'<div class="fig-value {c}">{v}</div>'
        f'<div class="fig-note">{n}</div></div>'
        for l, v, c, n in figs
    )

    reasons = []
    for reason, b in report["by_failure_reason"].items():
        pct = b["recovery_rate_percent"]
        reasons.append(
            f'<div class="reason"><div class="reason-name">{_esc(reason)}</div>'
            f'<div class="track"><div class="fill" style="width:{max(pct, 0.6)}%"></div></div>'
            f'<div class="reason-fig"><b>{b["recovered"]}/{b["count"]}</b> &middot; '
            f'{_money(b["recovered_amount"])} &middot; {pct}%</div></div>'
        )

    ledger = []
    for c in report["audit_log"]:
        src = c["decided_by"]
        chip = "guard" if src == "guardrail" else ("rules" if src == "fallback_rules" else "ai")
        outcome = c["outcome"]
        ocls = "ok" if outcome == "recovered" else "no"
        ledger.append(
            f'<tr><td class="pid">{_esc(c["payment_id"])}</td>'
            f'<td class="amt">{_money(c["amount"])}</td>'
            f'<td><span class="strategy">{_esc(c["failure_reason"])}</span><br>'
            f'<span class="conf">{_esc(c["customer_history"])} &middot; '
            f'{c["previous_successful_payments"]} prior</span></td>'
            f'<td><span class="strategy">{_esc(c["strategy"])}</span><br>'
            f'<span class="conf">{_esc(c["confidence"])}</span></td>'
            f'<td><span class="chip {chip}">{_esc(src)}</span></td>'
            f'<td class="why">{_esc(c["reasoning"])}</td>'
            f'<td><span class="chip {ocls}">{_esc(outcome.replace("_", " "))}</span></td></tr>'
        )

    compare_html = _render_compare(comparison) if comparison else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paytriage Recovery Statement</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&amp;family=IBM+Plex+Sans:wght@400;500;600&amp;family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&amp;display=swap">
<style>{HTML_STYLE}</style></head><body><div class="sheet">
<header class="masthead"><div>
<h1 class="wordmark">Paytriage <span>/ Recovery Statement</span></h1>
<div class="doctype">Failed recurring payments &middot; decision + outcome ledger</div>
</div><div class="runmeta">{meta_html}</div></header>
<div class="verdict">{figs_html}</div>
{compare_html}
<section><h2>Recovery by failure reason</h2>
<p class="sub">Share of payments recovered, per decline type.</p>
<div class="reasons">{"".join(reasons)}</div></section>
<section><h2>Decision ledger</h2>
<p class="sub">Every payment, who decided it, and why. Guardrail rows never reached the model.</p>
<div class="ledger"><table><thead><tr><th>Payment</th><th class="amt">Amount</th>
<th>Failure / segment</th><th>Decision</th><th>Decided by</th><th>Reasoning</th><th>Outcome</th>
</tr></thead><tbody>{"".join(ledger)}</tbody></table></div></section>
<section><div class="note caveat"><b>On reading these numbers.</b>
{_esc(meta["simulation_note"])} The rules path is exactly reproducible at a fixed seed; an AI run
varies between invocations because model decisions are not deterministic.</div></section>
<footer><span>paytriage &middot; generated by agent.py</span>
<span>Razorpay AI Buildathon &middot; Track 03 &middot; AI Revenue Recovery</span></footer>
</div></body></html>"""


def write_html(html_text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")

def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(report: dict[str, Any]) -> None:
    s = report["summary"]
    layer = report["metadata"]["decision_layer"]
    line = "-" * 62

    print()
    print("PAYTRIAGE - RECOVERY RUN")
    print(line)
    if layer["ai_enabled"]:
        counts = layer["decided_by"]
        by_model = counts.get(layer["provider"], 0)
        print(
            f"  Decisions                 {by_model} by {layer['model']}, "
            f"{counts.get('guardrail', 0)} by guardrail, "
            f"{counts.get('fallback_rules', 0)} by fallback rules"
        )
    else:
        print("  Decisions                 deterministic rule engine (--no-ai)")
    print()
    print(f"  Payments processed        {s['payments_processed']}")
    if s["malformed_records_skipped"]:
        print(f"  Malformed records skipped {s['malformed_records_skipped']}")
    print(f"  Amount at risk            INR {s['amount_at_risk']:,}")
    print()
    print(f"  Pursued                   {s['recovery_attempted']}")
    print(f"  Declined by policy        {s['recovery_declined']}")
    print(f"  Recovered                 {s['recovered_count']}")
    print()
    print(f"  Amount recovered          INR {s['recovered_amount']:,}")
    print(f"  Amount written off        INR {s['unrecovered_amount']:,}")
    print(f"  Amount recovery rate      {s['amount_recovery_rate_percent']}%")
    print(f"  Success rate when pursued {s['attempt_success_rate_percent']}%")
    print()
    print(f"  Customer touches          {s['total_customer_touches']}")
    print(f"  Avg touches when pursued  {s['avg_touches_per_pursued_payment']}")
    print(line)

    print("\nBy failure reason")
    for reason, b in report["by_failure_reason"].items():
        print(
            f"  {reason:<20} {b['recovered']:>2}/{b['count']:<3} "
            f"INR {b['recovered_amount']:>7,} recovered  "
            f"({b['recovery_rate_percent']}%)"
        )

    print("\nBy customer segment")
    for segment, b in report["by_customer_segment"].items():
        print(
            f"  {segment:<20} {b['recovered']:>2}/{b['count']:<3} "
            f"INR {b['recovered_amount']:>7,} recovered  "
            f"({b['recovery_rate_percent']}%)"
        )
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into the environment.

    Keeps secrets out of the command line and out of shell history. Written
    against the standard library rather than pulling in python-dotenv - the
    format we need is a few lines of parsing, and a submission that runs on a
    bare interpreter is worth more than the convenience.

    Values already set in the real environment win, so an explicit
    `export GEMINI_API_KEY=...` still overrides the file.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return  # unreadable .env is not worth failing a run over

    import os

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paytriage - triage and recover failed recurring payments.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "sample_payments.json",
        help="Path to the failed-payments JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "recovery_results.json",
        help="Where to write the recovery report.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for the outcome simulator. Same seed, same results.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Write the report without printing the summary.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip the model entirely and use the deterministic rule engine. Fully reproducible.",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "claude"],
        default="gemini",
        help="Which model provider decides the cases (default: gemini, which has a free tier).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the provider's default model ID.",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=None,
        metavar="PATH",
        help="Also write a self-contained HTML report to PATH (opens in any browser).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help=(
            "Run the rule engine and the AI on identical input and the same seed, "
            "and report the difference. Costs no extra API calls - the rules pass is free."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"Payments decided per request (default: {DEFAULT_BATCH_SIZE}). The free tier "
            f"meters requests, not payments, so batching is what fits a run inside quota. "
            f"Use 1 for one request per payment."
        ),
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_REQUESTS_PER_MINUTE,
        help=(
            f"Requests per minute to allow (default: {DEFAULT_REQUESTS_PER_MINUTE}, "
            f"tuned for the Gemini free tier). Raise on a paid tier; 0 disables pacing."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help=(
            "Parallel model requests (default: 4, tuned for free-tier rate limits). "
            "Raise it if you are on a paid tier."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Pick up GEMINI_API_KEY from a local .env before building any provider.
    load_dotenv(Path(__file__).parent / ".env")

    payments, rejected = load_payments(args.input)

    if not payments:
        print(f"No usable payments found in {args.input}.", file=sys.stderr)
        if rejected:
            print(f"{len(rejected)} record(s) were rejected:", file=sys.stderr)
            for r in rejected[:5]:
                print(f"  - {r}", file=sys.stderr)
        return 1

    if rejected:
        print(
            f"Warning: skipped {len(rejected)} malformed record(s). "
            f"Details are in the report under 'malformed_records'.",
            file=sys.stderr,
        )

    engine = PolicyEngine()
    decider: AIDecider | None = None

    key_hint = {
        "gemini": "GEMINI_API_KEY (get one free at aistudio.google.com/apikey)",
        "claude": "ANTHROPIC_API_KEY",
    }[args.provider]

    if not args.no_ai:
        provider, problem = build_provider(args.provider)
        if provider is None:
            print(
                f"{problem}\n"
                f"Falling back to the deterministic rule engine. "
                f"Set {key_hint}, or pass --no-ai to silence this.",
                file=sys.stderr,
            )
        else:
            if args.model:
                provider.model = args.model
            decider = AIDecider(provider, engine, requests_per_minute=args.rpm)

    if decider is not None:
        problem = decider.preflight()
        if problem:
            print(
                f"{args.provider} unavailable ({problem}).\n"
                f"Falling back to the deterministic rule engine for this run. "
                f"Set {key_hint}, or pass --no-ai to silence this.",
                file=sys.stderr,
            )
            decider = None
        else:
            eligible = sum(1 for p in payments if engine.check_guardrails(p) is None)
            n_requests = -(-eligible // max(1, args.batch_size))  # ceiling division
            print(
                f"Deciding {len(payments)} payment(s) with {decider.model}: "
                f"{eligible} reach the model in {n_requests} request(s) "
                f"of up to {args.batch_size}...",
                file=sys.stderr,
            )

    verdicts = decide_all(payments, engine, decider, max(1, args.concurrency), args.batch_size)

    simulator = RecoverySimulator(args.seed)
    results = [
        simulator.run(p, decision, decided_by)
        for p, (decision, decided_by) in zip(payments, verdicts)
    ]

    ai_failures = decider.failures if decider else []
    if ai_failures:
        print(
            f"Warning: {len(ai_failures)} AI decision(s) failed and fell back to rules. "
            f"Details are in the report under 'decision_layer.ai_failures'.",
            file=sys.stderr,
        )

    report = build_report(
        results,
        rejected,
        args.seed,
        ai_enabled=decider is not None,
        ai_failures=ai_failures,
        provider=decider.provider.name if decider else None,
        model=decider.model if decider else None,
        rate_limit_retries=decider.retries if decider else 0,
    )
    write_report(report, args.output)

    # --compare re-decides the same payments with the rule engine only. The
    # simulator is re-seeded identically, so any difference in outcome is
    # attributable to the decision layer and nothing else.
    comparison = None
    if args.compare:
        if not report["metadata"]["decision_layer"]["ai_enabled"]:
            print(
                "Nothing to compare: the AI layer did not run, so both sides "
                "would be the rule engine. Omit --no-ai and supply an API key.",
                file=sys.stderr,
            )
        else:
            rules_verdicts = decide_all(payments, engine, None, 1, args.batch_size)
            rules_sim = RecoverySimulator(args.seed)
            rules_results = [
                rules_sim.run(p, decision, decided_by)
                for p, (decision, decided_by) in zip(payments, rules_verdicts)
            ]
            rules_report = build_report(rules_results, rejected, args.seed)
            comparison = build_comparison(rules_report, report)

    if args.html:
        write_html(render_html(report, comparison), args.html)

    if not args.quiet:
        print_summary(report)
        if comparison:
            print_comparison(comparison, report["metadata"]["decision_layer"]["model"])
        print(f"Full audit trail written to {args.output}")
        if args.html:
            print(f"HTML report written to {args.html}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
