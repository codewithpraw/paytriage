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
