# What broke, and how it got fixed

Six things went wrong building Paytriage. Each one is here with what was
assumed, what actually happened, how it was found, and what changed. They are
in the order they occurred, because several were only reachable after the
previous one was fixed.

The through-line: every one of these was found by **measuring something**, not
by reasoning about it. The two worst bugs looked completely fine in the code.

---

## 1. The free tier allows 20 requests. The design needed 36.

**Assumed:** one API request per payment. 46 payments, 36 of them reaching the
model after guardrails, so 36 requests. Obvious, clean, one decision per call.

**What happened:** the run died about a third of the way through. Fourteen
payments got real decisions; the other twenty-two silently fell back to the
rule engine. The output looked superficially fine — every payment had a
verdict — which is exactly what made it dangerous. The report said
`decided_by: fallback_rules` twenty-two times and nothing crashed.

**How it was found:** sending one deliberate probe request and reading the raw
error instead of the SDK's exception type:

```
Quota exceeded for metric: generate_content_free_tier_requests,
limit: 20, model: gemini-3.7-flash
```

**The fix:** the free tier meters *requests*, not payments. So stop sending one
payment per request. Payments are now batched twelve to a call — the model
returns an array of decisions keyed by `payment_id` — and 36 payments cost
**3 requests** instead of 36.

Batching had been considered earlier and rejected as a premature optimisation,
on the assumption the constraint was *time*. It wasn't. It was quota, and no
amount of pacing fits 36 requests into an allowance of 20.

The per-payment safety properties had to survive the change: a payment the
model omits, mangles, or answers with an id that was never sent is failed
*individually* and filled in from the rule engine. One bad entry cannot poison
the other eleven.

---

## 2. A run that hung for two hours and thirty-seven minutes

**Assumed:** HTTP 429 means "you are going too fast." Back off, wait, try
again. Standard, and correct most of the time.

**What happened:** a run estimated at 3.6 minutes was still going 2h37m later.

**The cause:** 429 covers two different situations that need opposite
responses.

| Meaning | Right response |
|---|---|
| Sending too fast | Wait, then retry — it will clear |
| Daily allowance exhausted | Stop. Waiting will not help until tomorrow. |

This was the second one. The retry logic kept backing off against a quota that
was already gone. Worse, a rate limiter had been added to space requests out,
so every doomed retry *also* waited for its turn in the queue before sleeping
through its backoff. The two mitigations compounded each other into hours of
politely waiting at a door that was not going to open.

**The fix:** batching (above) removed the cause — three requests never
approach the limit — and retries are now bounded, so the worst case is a
couple of minutes rather than unbounded. The deeper lesson stands on its own:
**a retry is only correct if the thing you are retrying can succeed on the
next attempt.** Retrying an exhausted quota is not resilience, it is a loop.

---

## 3. The comparison feature reported "no difference" — and it was right to

This is the most instructive one, because the bug was invisible until a tool
was built specifically to look for it.

**Built:** `--compare`, which runs the rule engine and the AI on identical
input with the same seed and reports the difference. The whole point of the
project is that AI decisions beat fixed rules, so this is the number that
matters.

**What happened:** it reported a delta of **zero**. Same money recovered, same
rate, same everything — on a run where the model had demonstrably decided five
payments differently from the rules.

**The cause:** the simulator decided whether an attempt succeeded by looking at
the failure reason, the customer segment, and the attempt number. It never
looked at **which strategy had been chosen**. So `notify_then_retry` and
`immediate_retry` produced identical odds, both ladders ran three attempts, and
the choice between them was invisible.

The sharpest illustration is in a comment in the project's own code. The base
recovery rate for an expired card is `0.72`, annotated *"customer updates card
when prompted"* — the rate assumes you prompted them. But a silent retry
prompts nobody, and an expired card cannot clear until new details exist. Both
were being scored at 0.72.

The consequence: **a decision layer that reasoned correctly about strategy
scored exactly zero for it.** The measurement could not see the thing it
existed to measure.

**The fix:** `STRATEGY_FIT` prices whether a strategy actually addresses the
failure. Silently retrying an expired card now scores 21%; notifying first
scores 83%. Long-tenured customers keep an account-updater bonus, because
card networks do silently refresh stored details — and that is the exact case
both the rules and the model reason about explicitly, so the simulator has to
model it or that reasoning registers as noise.

---

## 4. Randomness leaked between payments

Found immediately after fixing #3, because the numbers still did not add up.

**What happened:** the comparison reported a total difference of +₹1,500, while
the sum of the individual payments that were decided differently came to
−₹1,400. Both figures were computed correctly. They disagreed because the
experiment was not controlled.

**The cause:** the simulator drew from a single shared `random.Random(seed)`,
consumed in order across all payments. Payment 1 takes some values, payment 2
takes the next ones, and so on.

Now change one decision so payment 1 makes three attempts instead of two. It
consumes one extra value — and **every payment after it draws from a different
position in the sequence**. Payments that both engines decided identically got
different luck, and the comparison attributed that noise to the decision layer.

**The fix:** each payment draws from its own stream, derived from
`f"{seed}:{payment_id}"`. A payment's outcome now depends only on its own
decision.

With #3 and #4 both fixed, the numbers reconcile exactly: nine payments decided
differently, swinging **+₹1,799**, which is the entire gap between the two
columns. That reconciliation is now asserted in the test suite — if the swings
ever stop adding up to the total, the A/B has silently broken again.

---

## 5. The chosen model provider cost money that did not exist

**Assumed:** Claude for the decision layer.

**Constraint discovered later:** a hard budget of zero. Anthropic has no free
tier — API credits must be purchased.

**The trap along the way:** a free "Gemini Pro" subscription looked like the
answer. It is not. Google's documentation is explicit that consumer AI plan
benefits *"apply only within the Google AI Studio web interface"* and that
direct API use is "billed and managed separately." A consumer subscription
grants no API access at all. The API free tier is a separate door, reached
through AI Studio, and it covers Flash models only — Pro models left the free
tier in April 2026.

**The fix:** the decision layer was made provider-agnostic. `GeminiProvider`
and `ClaudeProvider` handle transport only; all validation, fallback, and audit
logic lives once in `AIDecider`. Switching providers is a flag, not a rewrite.
The default is `gemini-3.5-flash-lite`, which stays inside the free tier.

---

## 6. A 400 that was misdiagnosed as a credentials problem

**Symptom:** every run failed at startup with
`responseFormat must be set when responseMimeType is set` — despite both
being set.

**First wrong turn:** the preflight check reported this as *"credentials
rejected,"* sending debugging toward the API key, which was fine. That
misleading message was itself a bug: a 400 is a malformed request, not an
authentication failure, and conflating them wasted time. The preflight now
distinguishes 401/403 (key problem) from 400 (request-shape problem) and says
which.

**The real cause:** two different exception hierarchies. The SDK's newer
`interactions` API raises from `google.genai._gaos.lib.compat_errors`, not from
`google.genai.errors` — so none of the carefully-written `except` clauses ever
matched, and the error escaped as an unhandled type. Errors are now classified
by HTTP status, which does not depend on which private module the SDK
reorganises next.

**And the request itself:** found by intercepting the serialised HTTP body
rather than guessing at the SDK's field names. Sending a top-level
`response_mime_type` *alongside* `response_format` is rejected; the mime type
belongs nested inside `response_format`. Reading what was actually sent on the
wire took two minutes and ended an hour of speculation.

---

## What this list has in common

Four of these six bugs produced **no error at all**. The run completed, the
report was generated, and the numbers looked plausible:

- 22 payments quietly decided by fallback rules instead of the AI
- a comparison confidently reporting a delta of zero
- per-payment swings that did not sum to the total
- a "successful" run that was measuring the wrong thing entirely

Only the 400 and the hang announced themselves. The rest were caught by
building something that checked, then noticing the check disagreed with what
was expected.

That is why `--compare` exists in the form it does, and why the test suite
asserts reconciliation rather than just "it runs." The most expensive bugs here
were not crashes. They were plausible-looking numbers.
