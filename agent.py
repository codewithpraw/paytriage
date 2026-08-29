"""Orchestrates the payment recovery batch run: load, decide, simulate, aggregate."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic

from claude_decider import decide_retry_strategy
from models import Payment, PaymentResult
from simulator import execute_decision

logger = logging.getLogger(__name__)


def load_payments(path: Path) -> tuple[list[Payment], list[dict[str, Any]]]:
    """Load and validate payments from a JSON file.

    Returns (valid_payments, skipped) where each `skipped` entry describes why
    a record was rejected. A malformed individual record does not abort
    loading - it's recorded and the rest of the file is still processed.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"input file not found: {path}") from exc
    except OSError as exc:
        raise OSError(f"could not read input file {path}: {exc}") from exc

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"input file {path} is not valid JSON: {exc}") from exc

    if isinstance(raw_data, dict) and "payments" in raw_data:
        raw_data = raw_data["payments"]
    if not isinstance(raw_data, list):
        raise ValueError(f"expected a JSON array of payments (or {{'payments': [...]}}) in {path}")

    payments: list[Payment] = []
    skipped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_data):
        if not isinstance(raw, dict):
            skipped.append({"index": index, "error": "record is not a JSON object"})
            continue
        try:
            payment = Payment.from_dict(raw)
        except ValueError as exc:
            skipped.append({"index": index, "payment_id": raw.get("payment_id"), "error": str(exc)})
            continue
        if payment.payment_id in seen_ids:
            skipped.append({"index": index, "payment_id": payment.payment_id, "error": "duplicate payment_id"})
            continue
        seen_ids.add(payment.payment_id)
        payments.append(payment)

    return payments, skipped


def _process_one(
    client: anthropic.Anthropic,
    payment: Payment,
    *,
    model: str,
    effort: str,
    rng: random.Random,
) -> PaymentResult:
    decision, source = decide_retry_strategy(client, payment, model=model, effort=effort)
    outcome = execute_decision(payment, decision, rng)
    return PaymentResult(payment=payment, decision=decision, decision_source=source, outcome=outcome)


def run_batch(
    payments: list[Payment],
    *,
    client: anthropic.Anthropic,
    model: str,
    effort: str,
    concurrency: int,
    seed: int,
) -> list[PaymentResult]:
    """Process every payment concurrently, isolating per-payment failures.

    A payment whose processing raises an unexpected exception (anything not
    already handled inside decide_retry_strategy) is logged and dropped from
    the results rather than aborting the rest of the batch.
    """
    results: list[PaymentResult] = []
    # Each payment gets its own RNG stream derived from the run seed, so
    # results are reproducible regardless of thread completion order.
    base_rng = random.Random(seed)
    payment_seeds = {p.payment_id: base_rng.getrandbits(64) for p in payments}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        future_to_payment = {
            pool.submit(
                _process_one,
                client,
                payment,
                model=model,
                effort=effort,
                rng=random.Random(payment_seeds[payment.payment_id]),
            ): payment
            for payment in payments
        }
        for future in concurrent.futures.as_completed(future_to_payment):
            payment = future_to_payment[future]
            try:
                results.append(future.result())
            except Exception:
                logger.exception("payment %s: unexpected failure processing batch item; dropping from results", payment.payment_id)

    order = {p.payment_id: i for i, p in enumerate(payments)}
    results.sort(key=lambda r: order[r.payment.payment_id])
    return results


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    count = bucket["count"]
    out = {
        "count": count,
        "amount_at_risk": _money(bucket["amount_at_risk"]),
        "amount_recovered": _money(bucket["amount_recovered"]),
    }
    if "succeeded" in bucket:
        out["success_rate"] = round(bucket["succeeded"] / count, 4) if count else 0.0
    return out


def build_metrics(
    results: list[PaymentResult],
    *,
    skipped_records: list[dict[str, Any]],
    input_file: str,
    model: str,
) -> dict[str, Any]:
    total_amount_at_risk = sum((r.payment.amount for r in results), Decimal("0"))
    total_recovered = sum((r.outcome.amount_recovered for r in results), Decimal("0"))
    attempted = [r for r in results if r.outcome.attempted]
    succeeded = [r for r in results if r.outcome.succeeded]

    by_strategy: dict[str, dict[str, Any]] = {}
    by_currency: dict[str, dict[str, Any]] = {}
    for r in results:
        sbucket = by_strategy.setdefault(
            r.decision.strategy,
            {"count": 0, "amount_at_risk": Decimal("0"), "amount_recovered": Decimal("0"), "succeeded": 0},
        )
        sbucket["count"] += 1
        sbucket["amount_at_risk"] += r.payment.amount
        sbucket["amount_recovered"] += r.outcome.amount_recovered
        sbucket["succeeded"] += 1 if r.outcome.succeeded else 0

        cbucket = by_currency.setdefault(
            r.payment.currency,
            {"count": 0, "amount_at_risk": Decimal("0"), "amount_recovered": Decimal("0"), "succeeded": 0},
        )
        cbucket["count"] += 1
        cbucket["amount_at_risk"] += r.payment.amount
        cbucket["amount_recovered"] += r.outcome.amount_recovered
        cbucket["succeeded"] += 1 if r.outcome.succeeded else 0

    return {
        "run_metadata": {
            "input_file": input_file,
            "model": model,
            "total_records_in_file": len(results) + len(skipped_records),
            "records_processed": len(results),
            "records_skipped": len(skipped_records),
            "skipped_records": skipped_records,
        },
        "summary": {
            "total_payments": len(results),
            "total_amount_at_risk": _money(total_amount_at_risk),
            "total_amount_recovered": _money(total_recovered),
            "attempted_count": len(attempted),
            "succeeded_count": len(succeeded),
            "overall_success_rate": round(len(succeeded) / len(results), 4) if results else 0.0,
            "recovery_rate_of_amount_at_risk": (
                float(round(total_recovered / total_amount_at_risk, 4)) if total_amount_at_risk else 0.0
            ),
            "decisions_from_fallback": sum(1 for r in results if r.decision_source == "fallback_default"),
        },
        "by_strategy": {k: _finalize_bucket(v) for k, v in sorted(by_strategy.items())},
        "by_currency": {k: _finalize_bucket(v) for k, v in sorted(by_currency.items())},
        "results": [_result_to_dict(r) for r in results],
    }


def _result_to_dict(result: PaymentResult) -> dict[str, Any]:
    p = result.payment
    return {
        "payment_id": p.payment_id,
        "customer_id": p.customer_id,
        "amount": _money(p.amount),
        "currency": p.currency,
        "failure_code": p.failure_code,
        "decision": {
            "strategy": result.decision.strategy,
            "retry_delay_hours": result.decision.retry_delay_hours,
            "max_additional_attempts": result.decision.max_additional_attempts,
            "customer_contact_recommended": result.decision.customer_contact_recommended,
            "confidence": result.decision.confidence,
            "reasoning": result.decision.reasoning,
        },
        "decision_source": result.decision_source,
        "outcome": {
            "attempted": result.outcome.attempted,
            "succeeded": result.outcome.succeeded,
            "amount_recovered": _money(result.outcome.amount_recovered),
            "notes": result.outcome.notes,
        },
    }


def write_results(metrics: dict[str, Any], output_path: Path) -> None:
    try:
        output_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"could not write output file {output_path}: {exc}") from exc
