# Architecture

This document explains how Paytriage is built and, more importantly, *why*
each part is built the way it is. For how to run it and what it produced,
see [README.md](README.md).

---

## Design goal

Separate three concerns that are usually tangled together in retry systems:

1. **Should we even try to recover this payment?**
2. **If yes, what's the right way to try?**
3. **Did it work, and when do we stop?**

Most naive retry logic collapses all three into "retry N times on a
schedule." Paytriage keeps them as three distinct stages so that each one can
be reasoned about, tested, and replaced independently.

## Module layout

```
                        ┌─ refused ──────────────────────────┐
                        │                                    v
Payment --> PolicyEngine.check_guardrails()          Decision --> RecoverySimulator.run() --> CaseResult
 (input)      (stage 1: hard refusals)                (verdict)        (stage 3)              (audit record)
                        │                                    ^
                        └─ allowed ─> AIDecider.decide() ─────┘
                                       (stage 2: judgment)
                                       falls back to
                                       PolicyEngine.decide()
```

**`Payment`** — a normalised, validated record. Malformed input (missing
fields, non-numeric amounts, duplicate IDs) is rejected here, before it can
reach the decision logic. Rejections are collected and reported, not silently
dropped and not fatal to the whole run.

**`PolicyEngine.check_guardrails()`** — the hard refusals, enforced in code
before any model sees the payment. Returns a refusal `Decision`, or `None`
meaning "safe to hand to the judgment layer." Keeping these out of the prompt
is the point: a guardrail that lives in a prompt is a request, while one that
lives in a branch is a guarantee. The model is never given the opportunity to
argue with a fraud signature.

**Batching.** `AIDecider.decide_batch()` sends up to 12 payments per request,
and the model returns an array of decisions keyed by `payment_id`. This is not
a speed optimisation — it is what makes the project runnable at all on a free
tier that meters *requests* rather than payments, with quotas as low as 20. One
request per payment cannot fit a 36-payment run into that; three requests can.

The per-payment safety properties survive batching. A payment the model omits,
returns with an invalid strategy, or answers with a `payment_id` that was never
sent is failed *individually* and filled in from the rule engine — one bad entry
does not poison the other eleven. `decide_all()` also asserts that no payment
escapes with a null verdict, whatever happened upstream.

**`AIDecider.decide_batch()`** — the judgment layer. Sends the payments to a model and
gets back a structured `Decision`. Its output is validated before use: an
unknown strategy, malformed JSON, a refusal, or a transport failure all route
to `PolicyEngine.decide()` instead, and the reason is recorded in
`decision_layer.ai_failures`. It never raises for a single payment, so one bad
response cannot take down a batch.

**Providers** — `GeminiProvider` and `ClaudeProvider` handle transport only:
given a system prompt and a payment brief, return text or raise
`ProviderError`. All validation and fallback logic lives in `AIDecider`, so it
is written once and applies identically no matter who answers. Gemini is the
default because its Flash models are on a free API tier, which keeps the
project runnable at zero cost; swapping providers is a flag, not a rewrite.

**`PolicyEngine.decide()`** — the full deterministic policy (guardrails plus
routing). It serves two roles: the fallback whenever the AI layer can't
produce a usable answer, and the entire decision layer under `--no-ai`. Having
a complete rule-based path that is always ready to take over is what makes
depending on a model safe.

**`RecoverySimulator.run()`** — takes a `Decision` and executes it: runs the
attempt ladder the strategy specifies, checks a hard global cap, and records
an outcome. This is the only place that touches randomness.

**`build_report()`** — pure aggregation. Takes a list of `CaseResult` and
produces the summary, the by-reason and by-segment breakdowns, and embeds the
full audit log. No decisions happen here; it only counts what already
happened.

Keeping these separate means a bug in the simulator can't hide inside the
policy, and a policy change can't accidentally change how outcomes are
scored.

---

## The decision rules, and why each one exists

### Refusal rules (checked first, in code, before the model)

These are the guardrails — cases where the answer is "don't pursue this,"
full stop, before any strategy is even considered. They run in
`check_guardrails()` ahead of the AI layer, so a payment matching one of them
is never sent to the model at all.

**Bank decline + fewer than 5 prior payments → refuse.**
A bank decline paired with thin account history is the standard signature of
a fraud hold or a compromised card, not a temporary glitch. Retrying doesn't
fix an issuer block, and pushing on it risks chargebacks and issuer
penalties that cost more than the payment is worth. This is the one rule
that overrides everything else, including a high payment amount.

**Fewer than 3 prior successful payments → refuse.**
Below this threshold there isn't enough signal that the customer intends to
keep paying at all. Chasing a payment from someone who may already be
churning is a retention problem, not a recovery one, and conflating the two
wastes recovery effort that should go to customers who've already shown
commitment.

**Lifetime value under ₹1,500 → refuse.**
Recovery isn't free — it costs a notification, a support ticket, or a
customer's patience. Below this floor, the expected value of the recovered
payment doesn't clearly exceed the cost of pursuing it.

### Routing rules (the fallback path, and what the model is asked to weigh)

These rules are what `PolicyEngine.decide()` applies when the AI layer isn't
available, and they also describe the reasoning the model is prompted to
perform. Documented together because they should stay in agreement — if the
model's judgment and the fallback diverge sharply on a case, that's worth
knowing about.

**Network error → immediate retry, no notification.**
This is a gateway-side fault. There's nothing the customer needs to do, so
there's nothing to notify them about — that would just be noise. Retry now.

**Card expired, long-tenured customer (15+ payments) → immediate retry.**
Card networks run account-updater services that silently refresh expired
card details on file. For customers with a long history, trying the retry
first often clears before the customer ever needs to be bothered. This is
the best possible outcome: recovery with zero customer friction.

**Card expired, shorter history → notify, then retry.**
Without the account-updater shortcut, the customer has to physically enter
new card details. Retrying before notifying just burns an attempt against a
card that structurally cannot succeed.

**Insufficient funds → notify, then retry, spaced across days.**
Usually a timing problem (payday, temporary cash flow) rather than an intent
problem. The retry delays are spread out deliberately — hammering an account
that's short on funds immediately after a decline just fails again for the
same reason.

**Bank decline, 5+ prior payments → route to a human, not a retry.**
A retry cannot clear a bank-side block. What's needed is a person who can
find out whether it's a fraud hold, a lapsed mandate, or a closed account —
things no automated retry logic can distinguish.

**Unrecognised failure reason → route to a human, low confidence.**
Rather than guess at an automated remedy for a reason the system has never
seen, it defers. Tested explicitly: an unknown reason doesn't crash the run
and doesn't get an invented strategy.

---

## The escalation ladder and stopping rules

Each strategy defines its own attempt schedule (how many attempts, spaced
how many days apart) — see `STRATEGIES` in `agent.py`. But every strategy is
capped by one number that sits above all of them: `MAX_ATTEMPTS = 3`. No
strategy, however it's configured, can push a customer past three contacts
for the same failed payment. That ceiling is the anti-spam guarantee, and
it's enforced in code (`delays[:MAX_ATTEMPTS]`), not just as a convention
strategies are supposed to follow.

Every attempt — success or failure — is written to the audit log with what
action was taken and why the sequence stopped (`recovered_on_attempt_2`,
`exhausted_attempt_budget_after_3`, `policy_declined_recovery`). Nothing
about a payment's fate is implicit.

---

## What's simulated, and how

There's no live payment gateway wired into this project. Whether a given
attempt succeeds is drawn from `RECOVERY_ODDS` — a base success probability
per failure reason (network errors resolve most often, bank declines least)
— adjusted by a segment multiplier (loyal customers are likelier to act on a
recovery prompt than lapsing ones) and discounted for each successive
attempt on the same payment (if the first two tries failed, the third rarely
saves it).

These numbers are stated assumptions, not measurements from real
transactions. They're gathered in one place at the top of `agent.py`
specifically so they're easy to find, question, and replace.

The simulation is **seeded** (`--seed`, default `42`). The same input and
seed always produce the same output — the numbers in the README aren't a
one-off lucky run, they're exactly what `python agent.py` produces for
anyone who clones the repo.

---

## Failure handling in the AI layer

Depending on a network call for every decision introduces failure modes a rule
engine doesn't have. Each is handled explicitly rather than allowed to
propagate:

| Failure | Handling |
|---|---|
| Rate limit (429) or 5xx | Retried with exponential backoff + jitter, up to 4 times |
| Rate limit still failing after retries | Fall back to rules, record the status |
| Other 4xx, connection failure | Fall back to rules immediately (retrying won't help) |
| Model refuses to answer | Fall back to rules, record `model_refused` |
| Output isn't valid JSON | Fall back to rules, record `invalid_json` |
| Output is JSON but not an object | Fall back to rules |
| JSON wrapped in a ```` ```json ```` fence | Fence stripped, decision kept |
| Strategy isn't one we defined | Fall back to rules, record the bad value |
| `should_recover` missing or non-boolean | Fall back to rules |
| Confidence outside high/medium/low | Coerced to `low`, decision kept |
| `should_recover: true` with strategy `skip` | Treated as the skip it describes |
| Blank reasoning | Placeholder inserted, decision kept |
| No credentials / SDK missing | Detected once up front; whole run uses rules |

The distinction matters: a malformed *field* in an otherwise sensible answer is
repaired, while a malformed *decision* is rejected outright. Every fallback is
counted in `decision_layer.ai_failures`, so a run that quietly degraded to
rules is visible in the output rather than looking like a successful AI run.

## What's explicitly out of scope (v1)

- No live Razorpay API calls. `RecoverySimulator` stands in for one.
- No real customer notifications. "Notify" is logged as an action, not sent.

Neither is hidden — the generated report carries `outcomes_are_simulated: true`
and the assumption values, so the limitation travels with the data, not just
with this document.

## Extension path

The remaining stand-in is the simulator. Replacing `RecoverySimulator.run()`'s
probability check with an actual Razorpay test-mode API call would make
outcomes as real as the decisions already are — and it's an isolated change,
because the `Decision` → `CaseResult` seam was drawn around exactly this
boundary from the start.
