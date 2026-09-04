#!/usr/bin/env python3
"""Tests for Paytriage.

Runs with no API key and no network: the decision layer is exercised through
fake providers that return canned text or raise. That matters because the
things most worth testing here are the failure paths - what happens when the
model is unreachable, returns nonsense, or invents a payment id.

    python test_agent.py            # all tests
    python test_agent.py -v         # per-test output

Standard library only. No pytest, no fixtures directory.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import agent
from agent import (
    MAX_ATTEMPTS,
    STRATEGIES,
    AIDecider,
    Decision,
    Payment,
    PolicyEngine,
    ProviderError,
    RecoverySimulator,
    build_comparison,
    build_report,
    decide_all,
    load_payments,
    render_html,
)

SAMPLE = Path(__file__).parent / "sample_payments.json"

# Backoff sleeps would make the retry tests take minutes; the delay itself is
# not what is under test.
agent.RATE_LIMIT_BASE_DELAY_SECONDS = 0.001


def make_payment(**overrides) -> Payment:
    """A valid payment, overridable per test."""
    base = {
        "payment_id": "pay_TEST01",
        "customer_id": "cust_TEST",
        "merchant_id": "merchant_test",
        "amount": 2499,
        "currency": "INR",
        "failure_reason": "insufficient_funds",
        "customer_history": "loyal",
        "subscription_type": "monthly",
        "previous_successful_payments": 18,
        "days_since_last_payment": 30,
        "customer_lifetime_value": 44982,
    }
    base.update(overrides)
    return Payment.from_dict(base)


def decision(strategy="immediate_retry", recover=True) -> Decision:
    return Decision(recover, strategy, "test reasoning", "high")


class FakeProvider:
    """Stands in for Gemini/Claude. Returns canned text, or raises."""

    name = "gemini"
    model = "fake-model"

    def __init__(self, behaviour="good"):
        self.behaviour = behaviour
        self.calls = 0
        self.batch_sizes: list[int] = []

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        ids = [l.split(": ", 1)[1] for l in user.splitlines() if l.startswith("payment_id: ")]
        self.batch_sizes.append(len(ids))

        if self.behaviour == "raise_rate_limit":
            raise ProviderError("rate_limited_429")
        if self.behaviour == "raise_bad_request":
            raise ProviderError("client_error_400")
        if self.behaviour == "raise_unexpected":
            raise RuntimeError("something nobody anticipated")
        if self.behaviour == "not_json":
            return "I am not JSON."
        if self.behaviour == "no_array":
            return '{"result": "ok"}'
        if self.behaviour == "half":
            ids = ids[: len(ids) // 2]
        if self.behaviour == "hallucinate":
            ids = ids + ["pay_NEVER_SENT"]

        entries = [
            {
                "payment_id": pid,
                "should_recover": True,
                "strategy": "immediate_retry",
                "reasoning": "fake reasoning",
                "confidence": "high",
            }
            for pid in ids
        ]
        if self.behaviour == "one_bad_strategy" and entries:
            entries[0]["strategy"] = "nuke_it_from_orbit"
        if self.behaviour == "fenced":
            return "```json\n" + json.dumps({"decisions": entries}) + "\n```"
        return json.dumps({"decisions": entries})


# ---------------------------------------------------------------------------


class TestInputValidation(unittest.TestCase):
    """Malformed rows must be rejected individually, never crash the run."""

    def test_loads_the_real_sample_file(self):
        payments, rejected = load_payments(SAMPLE)
        self.assertEqual(len(payments), 46)
        self.assertEqual(rejected, [])

    def test_missing_required_field_is_rejected(self):
        for field in ("payment_id", "amount", "failure_reason"):
            with self.subTest(field=field):
                raw = {"payment_id": "p", "amount": 100, "failure_reason": "network_error"}
                del raw[field]
                with self.assertRaises(ValueError):
                    Payment.from_dict(raw)

    def test_non_positive_and_non_numeric_amounts_rejected(self):
        for amount in (0, -100, "abc", None):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    make_payment(amount=amount)

    def test_unknown_extra_fields_are_ignored(self):
        # A richer upstream schema must not break us.
        p = make_payment(some_future_field="whatever")
        self.assertEqual(p.payment_id, "pay_TEST01")

    def test_bad_rows_are_skipped_not_fatal(self):
        import tempfile

        data = {
            "failed_payments": [
                {"payment_id": "ok_1", "amount": 100, "failure_reason": "network_error"},
                {"payment_id": "bad", "amount": -5, "failure_reason": "network_error"},
                "not even an object",
                {"payment_id": "ok_1", "amount": 100, "failure_reason": "network_error"},  # dupe
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        payments, rejected = load_payments(path)
        self.assertEqual(len(payments), 1)
        self.assertEqual(len(rejected), 3)


class TestGuardrails(unittest.TestCase):
    """Refusals are enforced in code and must not be reachable by the model."""

    def setUp(self):
        self.engine = PolicyEngine()

    def test_bank_decline_with_thin_history_is_refused(self):
        p = make_payment(failure_reason="bank_decline", previous_successful_payments=2,
                         customer_lifetime_value=50000)
        self.assertIsNotNone(self.engine.check_guardrails(p))

    def test_too_few_prior_payments_is_refused(self):
        p = make_payment(previous_successful_payments=1, customer_lifetime_value=50000)
        self.assertIsNotNone(self.engine.check_guardrails(p))

    def test_low_lifetime_value_is_refused(self):
        p = make_payment(customer_lifetime_value=100)
        self.assertIsNotNone(self.engine.check_guardrails(p))

    def test_good_customer_passes_to_the_model(self):
        self.assertIsNone(self.engine.check_guardrails(make_payment()))

    def test_refused_payments_never_reach_the_provider(self):
        """The whole point of guardrails: the model cannot see, or overrule, these."""
        refused = make_payment(payment_id="pay_REFUSED", failure_reason="bank_decline",
                               previous_successful_payments=0, customer_lifetime_value=0)
        provider = FakeProvider()
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        verdicts = decide_all([refused], self.engine, decider, concurrency=1, batch_size=12)
        dec, source = verdicts[0]
        self.assertEqual(provider.calls, 0, "provider was called for a guardrailed payment")
        self.assertEqual(source, "guardrail")
        self.assertFalse(dec.should_recover)


class TestAIDecisionLayer(unittest.TestCase):
    """Every unusable response must degrade to rules, never raise."""

    def setUp(self):
        self.engine = PolicyEngine()
        self.payments, _ = load_payments(SAMPLE)

    def run_with(self, behaviour, batch_size=12):
        provider = FakeProvider(behaviour)
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        verdicts = decide_all(self.payments, self.engine, decider,
                              concurrency=4, batch_size=batch_size)
        counts: dict[str, int] = {}
        for _, src in verdicts:
            counts[src] = counts.get(src, 0) + 1
        return provider, decider, verdicts, counts

    def test_good_response_is_used(self):
        _, decider, _, counts = self.run_with("good")
        self.assertEqual(counts.get("gemini"), 36)
        self.assertEqual(counts.get("guardrail"), 10)
        self.assertEqual(decider.failures, [])

    def test_every_failure_mode_degrades_to_rules(self):
        for behaviour in ("raise_rate_limit", "raise_bad_request", "raise_unexpected",
                          "not_json", "no_array"):
            with self.subTest(behaviour=behaviour):
                _, decider, verdicts, counts = self.run_with(behaviour)
                self.assertEqual(len(verdicts), 46, "every payment must still get a verdict")
                self.assertEqual(counts.get("fallback_rules"), 36)
                self.assertTrue(decider.failures)

    def test_partial_response_only_fails_the_missing_payments(self):
        _, decider, _, counts = self.run_with("half")
        self.assertTrue(0 < counts.get("gemini", 0) < 36)
        self.assertTrue(counts.get("fallback_rules", 0) > 0)
        self.assertTrue(all(f["error"] == "missing_from_batch_response"
                            for f in decider.failures))

    def test_invented_payment_id_is_ignored(self):
        """A model echoing an id we never sent must not be trusted."""
        _, _, _, counts = self.run_with("hallucinate")
        self.assertEqual(counts.get("gemini"), 36)

    def test_one_bad_entry_does_not_poison_its_batch(self):
        _, _, _, counts = self.run_with("one_bad_strategy")
        # 36 payments / 12 per batch = 3 batches, one bad entry each
        self.assertEqual(counts.get("fallback_rules"), 3)
        self.assertEqual(counts.get("gemini"), 33)

    def test_markdown_fenced_json_is_unwrapped(self):
        """Models wrap JSON in ```json fences even when given a schema."""
        _, _, _, counts = self.run_with("fenced")
        self.assertEqual(counts.get("gemini"), 36)

    def test_rate_limit_is_retried_then_succeeds(self):
        class Flaky(FakeProvider):
            def complete(self, system, user):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("rate_limited_429")
                return json.dumps({"decisions": []})

        provider = Flaky()
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        decider.decide_batch([make_payment()])
        self.assertEqual(provider.calls, 2, "a 429 should be retried, not surrendered")
        self.assertEqual(decider.retries, 1)

    def test_bad_request_is_not_retried(self):
        """Retrying a malformed request just wastes quota."""
        provider = FakeProvider("raise_bad_request")
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        decider.decide_batch([make_payment()])
        self.assertEqual(provider.calls, 1)


class TestBatching(unittest.TestCase):
    """Batching is what fits a run inside a 20-request free tier."""

    def setUp(self):
        self.engine = PolicyEngine()
        self.payments, _ = load_payments(SAMPLE)

    def test_batching_collapses_36_payments_into_3_requests(self):
        provider = FakeProvider()
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        decide_all(self.payments, self.engine, decider, concurrency=4, batch_size=12)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(sorted(provider.batch_sizes), [12, 12, 12])

    def test_batch_size_one_is_one_request_per_payment(self):
        provider = FakeProvider()
        decider = AIDecider(provider, self.engine, requests_per_minute=0)
        decide_all(self.payments, self.engine, decider, concurrency=4, batch_size=1)
        self.assertEqual(provider.calls, 36)


class TestSimulator(unittest.TestCase):
    def test_attempt_ceiling_is_never_exceeded(self):
        """No strategy may push a customer past MAX_ATTEMPTS contacts."""
        sim = RecoverySimulator(42)
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                result = sim.run(make_payment(), decision(strategy), "test")
                self.assertLessEqual(len(result.attempts), MAX_ATTEMPTS)

    def test_skip_attempts_nothing(self):
        result = RecoverySimulator(42).run(make_payment(), decision("skip", recover=False), "test")
        self.assertEqual(result.attempts, [])
        self.assertEqual(result.recovered_amount, 0)

    def test_strategy_changes_the_odds(self):
        """Regression: odds once ignored strategy, so good reasoning scored zero."""
        sim = RecoverySimulator(42)
        expired = make_payment(failure_reason="card_expired", previous_successful_payments=8)
        notify = sim._odds(expired, 1, "notify_then_retry")
        silent = sim._odds(expired, 1, "immediate_retry")
        self.assertGreater(notify, silent * 2,
                           "notifying should beat silently retrying an expired card")

    def test_account_updater_helps_long_tenured_customers(self):
        sim = RecoverySimulator(42)
        short = make_payment(failure_reason="card_expired", previous_successful_payments=5)
        long = make_payment(failure_reason="card_expired", previous_successful_payments=25)
        self.assertGreater(sim._odds(long, 1, "immediate_retry"),
                           sim._odds(short, 1, "immediate_retry"))

    def test_same_seed_reproduces_identical_outcomes(self):
        payments, _ = load_payments(SAMPLE)
        engine = PolicyEngine()
        decisions = [engine.decide(p) for p in payments]

        def run():
            sim = RecoverySimulator(42)
            return [sim.run(p, d, "x").outcome for p, d in zip(payments, decisions)]

        self.assertEqual(run(), run())

    def test_one_payment_cannot_disturb_another(self):
        """Regression: a shared RNG let one decision shift every later payment's luck."""
        payments, _ = load_payments(SAMPLE)
        engine = PolicyEngine()
        base = [engine.decide(p) for p in payments]

        def outcomes(decisions):
            sim = RecoverySimulator(42)
            return [sim.run(p, d, "x").outcome for p, d in zip(payments, decisions)]

        before = outcomes(base)
        idx = next(i for i, d in enumerate(base)
                   if d.should_recover and d.strategy == "notify_then_retry")
        mutated = list(base)
        mutated[idx] = decision("immediate_retry")
        after = outcomes(mutated)

        moved = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertIn(moved, ([], [idx]),
                      f"changing payment {idx} also moved {set(moved) - {idx}}")


class TestReporting(unittest.TestCase):
    def setUp(self):
        self.payments, _ = load_payments(SAMPLE)
        self.engine = PolicyEngine()
        verdicts = decide_all(self.payments, self.engine, None, 1, 12)
        sim = RecoverySimulator(42)
        self.results = [sim.run(p, d, s) for p, (d, s) in zip(self.payments, verdicts)]
        self.report = build_report(self.results, [], 42)

    def test_totals_reconcile_with_the_audit_log(self):
        s = self.report["summary"]
        self.assertEqual(s["recovered_amount"],
                         sum(c["recovered_amount"] for c in self.report["audit_log"]))
        self.assertEqual(s["amount_at_risk"],
                         sum(c["amount"] for c in self.report["audit_log"]))
        self.assertEqual(s["recovered_count"],
                         sum(1 for c in self.report["audit_log"] if c["outcome"] == "recovered"))

    def test_every_record_says_who_decided_it(self):
        for case in self.report["audit_log"]:
            self.assertIn(case["decided_by"], {"gemini", "claude", "guardrail", "fallback_rules"})
            self.assertTrue(case["reasoning"].strip())

    def test_report_declares_that_outcomes_are_simulated(self):
        """The limitation must travel with the data, not just the docs."""
        self.assertTrue(self.report["metadata"]["outcomes_are_simulated"])

    def test_comparison_swings_reconcile_against_the_total(self):
        """If they don't add up, the A/B is measuring noise."""
        ai_verdicts = decide_all(self.payments, self.engine, None, 1, 12)
        sim = RecoverySimulator(42)
        ai_results = [sim.run(p, d, s) for p, (d, s) in zip(self.payments, ai_verdicts)]
        ai_report = build_report(ai_results, [], 42)

        comparison = build_comparison(self.report, ai_report)
        total_delta = ai_report["summary"]["recovered_amount"] - \
            self.report["summary"]["recovered_amount"]
        self.assertEqual(comparison["net_swing"], total_delta)

    def test_html_report_renders_and_escapes(self):
        html_text = render_html(self.report)
        self.assertTrue(html_text.startswith("<!doctype html>"))
        self.assertIn("Decision ledger", html_text)
        # Model-written reasoning is untrusted text and must be escaped.
        evil = json.loads(json.dumps(self.report))
        evil["audit_log"][0]["reasoning"] = '<script>alert("xss")</script>'
        self.assertNotIn("<script>alert", render_html(evil))


if __name__ == "__main__":
    unittest.main(verbosity=2)
