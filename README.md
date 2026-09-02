# Paytriage

An AI-track payment recovery agent built for the **Razorpay AI Buildathon**
(AI Revenue Recovery track).

Paytriage triages failed recurring payments — deciding which ones are worth
pursuing, choosing a recovery strategy for each, and running a bounded,
auditable recovery sequence instead of retrying everything the same way.

---

## The problem

Recurring payments fail for a handful of recurring reasons: a card expires,
an account is briefly short on funds, a gateway hiccups, a bank blocks the
charge. Most retry systems treat all of these the same way — same number of
attempts, same timing, regardless of whether the failure is even recoverable.

That wastes attempts on payments that were never going to clear, and annoys
customers who get retried past the point of usefulness.

## What Paytriage does

For every failed payment, it decides three things:

1. **Is this worth pursuing at all?** New customers with no track record,
   low-lifetime-value accounts, and bank declines from thin-history customers
   are declined outright — recovery effort has a cost, and not every payment
   is worth it.
2. **What's the right strategy?** A network error gets an immediate retry
   (nothing for the customer to do). An expired card gets a notification
   first (the customer has to act). A bank decline gets routed to a human
   (a retry can't clear an issuer block).
3. **When does it stop?** Every payment gets a hard ceiling of 3 attempts,
   full stop. No strategy can exceed it. That's the anti-spam guarantee.

Every decision is logged with the reason it fired, so the output is a full
audit trail, not just a pass/fail count.

## How decisions get made

A hosted model makes the judgment call on each payment, returning a structured
verdict: pursue or skip, which strategy, why, and how confident it is. The
default is Google Gemini Flash, which runs on a free API tier — the whole
project costs nothing to run. `--provider claude` is also supported for anyone
with Anthropic credits. Two things constrain the model:

**Guardrails run first, in code.** The fraud-signature and
not-worth-pursuing refusals are enforced *before* the model is consulted —
those payments never reach it, and it has no way to overrule them. Safety
rules you can read and test beat safety rules written into a prompt.

**The attempt ceiling is enforced after.** Whatever strategy comes back,
`MAX_ATTEMPTS = 3` caps the ladder.

If the API is unreachable or returns something unusable, that payment falls
back to the deterministic rule engine rather than failing the run. Every audit
record carries `decided_by` (the provider name, `guardrail`, or
`fallback_rules`), so the split is always visible in the output — the report
never implies the model decided something it didn't.

`--no-ai` runs the rule engine alone, with no API calls at all.

## What's real and what's simulated

**Real:** the decision layer, the guardrails, the escalation ladder, the
stopping rules, and the audit trail. This is the actual product logic.

**Simulated:** whether any individual retry attempt succeeds. There's no live
payment gateway wired in, so outcomes are drawn from stated per-failure-reason
success rates (see `RECOVERY_ODDS` in `agent.py`), adjusted by customer
segment. Those rates are assumptions, not measurements from real transactions.

The attempt simulation is seeded (`--seed`, default `42`). Note that the
model's decisions aren't themselves deterministic, so an AI run can shift
slightly between invocations; `--no-ai` is fully reproducible and is what the
table below was generated with.

## Results

Both paths were run against `sample_payments.json` (46 synthetic
failed-payment records). The AI column is `python agent.py`; the rules column
is `python agent.py --no-ai`, which is exactly reproducible and serves as the
baseline the AI layer is measured against.

| Metric | Rules only | With AI (`gemini-3.5-flash-lite`) |
|---|---|---|
| Payments processed | 46 | 46 |
| Amount at risk | ₹63,204 | ₹63,204 |
| Pursued | 36 | 35 |
| Declined by guardrails | 10 | 10 |
| Recovered | 30 | 29 |
| Amount recovered | ₹33,070 | **₹35,871** |
| Amount recovery rate | 52.3% | **56.8%** |
| Success rate when pursued | 83.3% | 82.9% |
| Avg attempts per pursued payment | 1.47 | 1.57 |

By failure reason, with AI decisions:

| Reason | Recovered | Amount | Rate |
|---|---|---|---|
| network_error | 8/8 | ₹4,592 | 100% |
| card_expired | 10/15 | ₹11,490 | 66.7% |
| insufficient_funds | 10/16 | ₹17,790 | 62.5% |
| bank_decline | 1/7 | ₹1,999 | 14.3% |

All 36 eligible payments were decided by the model — no fallbacks, no
rate-limit retries — in **3 API requests**, because payments are batched.

### These AI numbers are one run, not a fixed result

The rules column is exactly reproducible: same seed, same output, forever. The
AI column is not, because model decisions vary between invocations. Two
consecutive runs on identical input produced ₹35,871 (56.8%) and ₹36,569
(57.9%) — the model chose `skip` once in one run and not the other. The table
above is the run currently saved in `recovery_results.json`; expect roughly
57% ± 1 rather than an exact figure.

This is why `--no-ai` exists and why the seed matters: it separates *simulator*
randomness, which is controlled, from *model* variability, which is not.

### Where the AI diverges from the rules, and the honest caveat

The rule engine routes every established-customer bank decline to a human. The
model retried one instead and recovered ₹1,999 — the single largest source of
the gap between the two columns.

It also generalised a rule rather than copying it. On `pay_KDL9H3K` — an
expired card for a customer with 12 prior payments — it reasoned that an
account-updater refresh might clear the charge without bothering the customer.
The hand-written rule only applies that shortcut at 15+ payments; the model
reached the same insight and applied it a threshold lower.

**The caveat:** the AI is more aggressive than the policy it replaced. It
pursues bank declines the rules deliberately escalate to humans, and the
simulator rewards pursuit without modelling chargeback cost, issuer penalties,
or support load. So the higher number is not straightforwardly "better" — it is
a different risk appetite, measured by a scorer that does not price risk. The
guardrails that matter still held: all 10 fraud-signature and
not-worth-pursuing refusals were enforced in code and never reached the model.

## Running it

Get a free Gemini API key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no credit
card required.

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key
python agent.py
```

This reads `sample_payments.json` and writes
`recovery_results.json`.

### Cost

Running this is free. The free tier needs no credit card, and with no billing
account attached there is no mechanism to charge you — Google's billing docs
state that charges only begin after you deliberately link a billing account and
prepay. **So don't enable billing on that project:** doing so moves it to the
paid tier permanently, and every call bills from the first token.

**Free-tier quota is metered in requests, and it is small** — some Flash models
allow as few as 20. That is the constraint this project is designed around, and
it drives two defaults:

- **Payments are batched**, 12 per request (`--batch-size`). The model returns
  one decision per payment, keyed by `payment_id`. So a 46-record run costs
  **3 requests, not 36** — the difference between fitting in the free tier and
  exhausting it a third of the way through.
- **The default model is `gemini-3.5-flash-lite`**, not the newest Flash. Lite
  is faster in practice and more generously provisioned on the free tier. Pro
  models left the free tier entirely in April 2026.

Check your account's live limits at
[aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit). If you
have paid quota, `--model` and `--batch-size 1` restore one request per payment.

To run with no API key at all — deterministic rule engine, no external calls:

```bash
python agent.py --no-ai
```

Options:

```bash
python agent.py --input sample_payments.json \
                 --output recovery_results.json \
                 --provider gemini \
                 --seed 42 \
                 --concurrency 8
```

`--provider` selects `gemini` (default, free tier) or `claude` (needs paid
Anthropic credits). `--model` overrides the model ID. `--seed` controls the
outcome simulator. `--concurrency` sets how many decisions run in parallel.
Without a usable key the run degrades to the rule engine and says so, rather
than failing.

## Project structure

```
paytriage/
├── agent.py                  # guardrails + AI decision layer + simulator
├── sample_payments.json      # 46 synthetic failed-payment records
├── recovery_results.json     # generated by agent.py — do not hand-edit
├── requirements.txt
├── architecture.md           # decision logic, explained
└── README.md
```

## What's next

`RecoverySimulator` is the remaining stand-in: swapping it for real Razorpay
test-mode API calls would make the outcomes as real as the decisions already
are, without touching the decision layer, the audit trail, or the aggregation
code. The seam is deliberate — `Decision` in, `CaseResult` out.

Built for Razorpay's AI Buildathon, AI Revenue Recovery track.
