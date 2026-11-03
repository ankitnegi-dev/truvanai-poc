"""Deterministic LangGraph monitor for bank telemetry.

The bank sends OpenTelemetry spans to the OTLP receiver. This module reads
those spans from PostgreSQL, compares them with the policy that TruvanAI has
on file for the tenant, writes a verdict to the audit trail, stores a row in
governance_violations, and alerts the tenant owner.

The graph has no language model. Every decision is deterministic Python:

    load_spans -> load_policy -> evaluate_rules -> write_verdicts -> notify -> END

Rules
-----
R0_insufficient_evidence
    A decision span is missing loan.amount or loan.application_id. TruvanAI
    cannot verify the policy, so the verdict is PENDING / UNKNOWN.

R1_high_value_auto_approval
    The bank auto-approved an amount above the tenant policy threshold. The
    verdict is BLOCKED / NO with severity HIGH.

R2_amount_inconsistent_in_trace
    loan.amount differs across spans of one trace. The decision cannot be
    trusted, so the verdict is BLOCKED / NO with severity HIGH.

A decision span that breaks no rule produces an ALLOWED / YES audit row with
the action governance_check_passed.

The threshold comes only from tenant_policies. It is never read from a span
attribute, so a bank cannot loosen the check by changing its own telemetry.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

import httpx

from langgraph.graph import END, START, StateGraph

from shared.db import log_action
from shared.telemetry_store import (
    get_policy,
    get_tenant,
    get_tenants_with_unevaluated_spans,
    get_trace_spans,
    get_unevaluated_trace_ids,
    has_governance_audit,
    mark_evaluated,
    record_violation,
    update_violation_alert,
    update_violation_containment,
)
from tools.notify import alerts_enabled, send_governance_alert

logger = logging.getLogger("truvanai.telemetry_monitor")

AGENT_NAME = "truvanai_monitor"
VIOLATION_ACTION = "governance_violation_detected"
COMPLIANT_ACTION = "governance_check_passed"
ENFORCEMENT = "detected_after_execution"
CONTAINMENT_ACTION = "containment_action"

RULE_R0 = "R0_insufficient_evidence"
RULE_R1 = "R1_high_value_auto_approval"
RULE_R2 = "R2_amount_inconsistent_in_trace"
RULE_COMPLIANT = "compliant_decision"
CONTAINMENT_RULES = frozenset({RULE_R1, RULE_R2})

DECISION_AUTO_APPROVED = "AUTO_APPROVED"
DECISION_SENT_TO_HITL = "SENT_TO_HITL"
DECISION_VALUES = frozenset({DECISION_AUTO_APPROVED, DECISION_SENT_TO_HITL})

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_NONE = "NONE"


class TelemetryState(TypedDict, total=False):
    """State shared by the telemetry monitor graph nodes."""

    tenant_id: str
    trace_ids: list[str]
    spans: list[dict[str, Any]]
    policy: dict[str, Any] | None
    owner_email: str | None
    control_url: str | None
    verdicts: list[dict[str, Any]]
    new_violations: list[dict[str, Any]]
    compliant_checks: int
    alerts_sent: list[str]
    alerts_failed: list[str]
    alerts_disabled: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _decimal_amount(value: Any) -> Decimal | None:
    """Return a finite Decimal from a span attribute value, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount


def _application_id(attributes: dict[str, Any]) -> str | None:
    """Return a non-empty loan application id from span attributes."""
    value = attributes.get("loan.application_id")
    if value is None:
        return None
    clean_value = str(value).strip()
    return clean_value or None


def _money_text(value: Decimal | None) -> str:
    """Format a Decimal for a plain sentence without a currency symbol."""
    if value is None:
        return "unknown"
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    """Return a span's attributes as a dictionary."""
    value = span.get("attributes")
    if isinstance(value, dict):
        return value
    return {}


def _decision_of(span: dict[str, Any]) -> str | None:
    """Return the loan decision carried by a span, or None."""
    value = _attributes(span).get("loan.decision")
    if isinstance(value, str) and value in DECISION_VALUES:
        return value
    return None


def _audit_parameters(verdict: dict[str, Any]) -> dict[str, Any]:
    """Build the JSONB parameters for one monitor audit row."""
    amount = verdict.get("amount")
    threshold = verdict.get("threshold")
    return {
        "tenant_id": verdict["tenant_id"],
        "trace_id": verdict["trace_id"],
        "span_id": verdict["span_id"],
        "application_id": verdict.get("application_id"),
        "amount": float(amount) if amount is not None else None,
        "threshold": float(threshold) if threshold is not None else None,
        "rule": verdict["rule"],
        "severity": verdict["severity"],
        "reason": verdict["reason"],
        "enforcement": ENFORCEMENT,
    }


def _make_verdict(
    *,
    tenant_id: str,
    trace_id: str,
    span_id: str,
    application_id: str | None,
    amount: Decimal | None,
    threshold: Decimal,
    currency: str,
    rule: str,
    severity: str,
    reason: str,
    policy_status: str,
    verdict_card: str,
    kind: str,
) -> dict[str, Any]:
    """Build one in-memory verdict for the write and notify nodes."""
    return {
        "kind": kind,
        "tenant_id": tenant_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "application_id": application_id,
        "amount": amount,
        "threshold": threshold,
        "currency": currency,
        "rule": rule,
        "severity": severity,
        "reason": reason,
        "policy_status": policy_status,
        "verdict_card": verdict_card,
        "action_name": (
            VIOLATION_ACTION if kind == "violation" else COMPLIANT_ACTION
        ),
    }


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def load_spans(state: TelemetryState) -> dict[str, Any]:
    """Load every stored span for the requested tenant and trace IDs."""
    spans = await get_trace_spans(state["tenant_id"], state["trace_ids"])
    return {"spans": spans}


async def load_policy(state: TelemetryState) -> dict[str, Any]:
    """Load the tenant policy and the owner email used for alerts."""
    tenant_id = state["tenant_id"]
    policy = await get_policy(tenant_id)
    tenant = await get_tenant(tenant_id)

    if policy is None:
        logger.warning(
            "Tenant %s has no tenant_policies row. Spans stay unevaluated.",
            tenant_id,
        )

    return {
        "policy": policy,
        "owner_email": tenant["owner_email"] if tenant else None,
        "control_url": tenant["control_url"] if tenant else None,
    }


def _evaluate_trace(
    tenant_id: str,
    trace_id: str,
    spans: list[dict[str, Any]],
    threshold: Decimal,
    currency: str,
) -> list[dict[str, Any]]:
    """Apply R0, R1, R2, and the compliant path to one trace."""
    decision_spans: list[tuple[dict[str, Any], str]] = []
    amounts: list[Decimal] = []

    for span in spans:
        attributes = _attributes(span)
        amount = _decimal_amount(attributes.get("loan.amount"))
        if amount is not None:
            amounts.append(amount)
        decision = _decision_of(span)
        if decision is not None:
            decision_spans.append((span, decision))

    # A trace without a decision span is incomplete, not a violation. It is
    # marked evaluated and will be evaluated again when the decision arrives.
    if not decision_spans:
        return []

    verdicts: list[dict[str, Any]] = []
    violation_found = False
    primary_span: dict[str, Any] | None = None

    # R0: every decision span must carry the amount and the application id.
    missing_evidence: list[tuple[dict[str, Any], list[str]]] = []
    for span, _decision in decision_spans:
        attributes = _attributes(span)
        missing: list[str] = []
        if _decimal_amount(attributes.get("loan.amount")) is None:
            missing.append("loan.amount")
        if _application_id(attributes) is None:
            missing.append("loan.application_id")
        if missing:
            missing_evidence.append((span, missing))

    if missing_evidence:
        span, missing = missing_evidence[0]
        primary_span = primary_span or span
        reason = (
            f"The decision span {span['name']} is missing "
            f"{', '.join(missing)}, so TruvanAI cannot verify the policy "
            f"threshold of {_money_text(threshold)} {currency}."
        )
        verdicts.append(
            _make_verdict(
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=str(span["span_id"]),
                application_id=_application_id(_attributes(span)),
                amount=_decimal_amount(
                    _attributes(span).get("loan.amount")
                ),
                threshold=threshold,
                currency=currency,
                rule=RULE_R0,
                severity=SEVERITY_MEDIUM,
                reason=reason,
                policy_status="PENDING",
                verdict_card="UNKNOWN",
                kind="violation",
            )
        )
        violation_found = True

    # R1: an amount above the tenant policy threshold must not be auto-approved.
    for span, decision in decision_spans:
        if decision != DECISION_AUTO_APPROVED:
            continue
        attributes = _attributes(span)
        amount = _decimal_amount(attributes.get("loan.amount"))
        application_id = _application_id(attributes)
        if amount is None or application_id is None:
            continue
        if amount <= threshold:
            continue
        primary_span = primary_span or span
        reason = (
            f"Loan {application_id} for {_money_text(amount)} was "
            f"auto-approved, but the policy for this tenant requires human "
            f"review above {_money_text(threshold)}."
        )
        verdicts.append(
            _make_verdict(
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=str(span["span_id"]),
                application_id=application_id,
                amount=amount,
                threshold=threshold,
                currency=currency,
                rule=RULE_R1,
                severity=SEVERITY_HIGH,
                reason=reason,
                policy_status="BLOCKED",
                verdict_card="NO",
                kind="violation",
            )
        )
        violation_found = True
        break

    # R2: all spans of one trace must agree on the amount.
    distinct_amounts = sorted(set(amounts))
    if len(distinct_amounts) > 1:
        span = primary_span or decision_spans[0][0]
        attributes = _attributes(span)
        amounts_text = ", ".join(
            _money_text(amount) for amount in distinct_amounts
        )
        reason = (
            f"Trace {trace_id} reports inconsistent loan amounts "
            f"({amounts_text}) across spans, so the decision cannot be "
            f"trusted."
        )
        verdicts.append(
            _make_verdict(
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=str(span["span_id"]),
                application_id=_application_id(attributes),
                amount=distinct_amounts[-1],
                threshold=threshold,
                currency=currency,
                rule=RULE_R2,
                severity=SEVERITY_HIGH,
                reason=reason,
                policy_status="BLOCKED",
                verdict_card="NO",
                kind="violation",
            )
        )
        violation_found = True

    # Compliant path: a decision span exists and no rule above fired.
    if not violation_found:
        span, decision = decision_spans[0]
        attributes = _attributes(span)
        amount = _decimal_amount(attributes.get("loan.amount"))
        application_id = _application_id(attributes) or "unknown"
        if decision == DECISION_AUTO_APPROVED:
            reason = (
                f"Loan {application_id} for {_money_text(amount)} was "
                f"auto-approved within the policy threshold of "
                f"{_money_text(threshold)}."
            )
        else:
            reason = (
                f"Loan {application_id} for {_money_text(amount)} was sent "
                f"to human review, which satisfies the policy threshold of "
                f"{_money_text(threshold)}."
            )
        verdicts.append(
            _make_verdict(
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=str(span["span_id"]),
                application_id=application_id,
                amount=amount,
                threshold=threshold,
                currency=currency,
                rule=RULE_COMPLIANT,
                severity=SEVERITY_NONE,
                reason=reason,
                policy_status="ALLOWED",
                verdict_card="YES",
                kind="compliant",
            )
        )

    return verdicts


async def evaluate_rules(state: TelemetryState) -> dict[str, Any]:
    """Apply the deterministic rules to every trace in the batch."""
    policy = state.get("policy")
    if policy is None:
        return {"verdicts": [], "errors": ["tenant policy is missing"]}

    threshold = _decimal_amount(policy.get("approval_threshold"))
    currency = str(policy.get("currency") or "INR")
    if threshold is None:
        return {"verdicts": [], "errors": ["tenant policy threshold is invalid"]}

    spans_by_trace: dict[str, list[dict[str, Any]]] = {}
    for span in state.get("spans", []):
        spans_by_trace.setdefault(str(span["trace_id"]), []).append(span)

    verdicts: list[dict[str, Any]] = []
    for trace_id in state["trace_ids"]:
        spans = spans_by_trace.get(trace_id, [])
        if not spans:
            continue
        verdicts.extend(
            _evaluate_trace(
                state["tenant_id"],
                trace_id,
                spans,
                threshold,
                currency,
            )
        )

    return {"verdicts": verdicts}


async def _attempt_containment(
    violation_id: int,
    verdict: dict[str, Any],
    control_url: str | None,
) -> None:
    """Call the tenant control URL for an R1 or R2 violation.

    The containment status on the violation row is updated to requested,
    then succeeded or failed. A missing URL or secret leaves the row as
    not_configured and writes no containment audit row.
    """
    secret = os.getenv("BANK_CONTROL_SECRET", "").strip()
    application_id = verdict.get("application_id")

    if not control_url or not secret or not application_id:
        await update_violation_containment(violation_id, "not_configured")
        return

    await update_violation_containment(violation_id, "requested")

    url = control_url.rstrip("/") + "/control/hold/" + str(application_id)
    status = "failed"
    error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                headers={"x-bank-control-secret": secret},
            )
        if 200 <= response.status_code < 300:
            status = "succeeded"
        else:
            error = f"HTTP {response.status_code}: {response.text[:500]}"
    except Exception as exc:  # noqa: BLE001 - containment must not abort verdicts
        error = str(exc)[:500]

    await update_violation_containment(violation_id, status)
    await log_action(
        agent_name=AGENT_NAME,
        action_name=CONTAINMENT_ACTION,
        parameters={
            "tenant_id": verdict["tenant_id"],
            "trace_id": verdict["trace_id"],
            "application_id": application_id,
            "control_url": control_url,
            "rule": verdict["rule"],
            "containment_status": status,
            "error": error,
        },
        result=(
            f"Containment {status} for {application_id}."
            + (f" Error: {error}" if error else "")
        ),
        policy_status="ALLOWED",
        verdict_card="YES",
    )


async def write_verdicts(state: TelemetryState) -> dict[str, Any]:
    """Persist violations and compliant checks, then mark spans evaluated.

    The governance_violations unique constraint on (tenant_id, trace_id, rule)
    makes the violation path idempotent. The compliant path uses an audit
    existence check so a repeated evaluation cannot append a second row.
    """
    new_violations: list[dict[str, Any]] = []
    compliant_checks = 0

    for verdict in state.get("verdicts", []):
        if verdict["kind"] == "violation":
            violation_id = await record_violation(
                tenant_id=verdict["tenant_id"],
                trace_id=verdict["trace_id"],
                span_id=verdict["span_id"],
                rule=verdict["rule"],
                severity=verdict["severity"],
                reason=verdict["reason"],
                application_id=verdict.get("application_id"),
                amount=verdict.get("amount"),
                threshold=verdict.get("threshold"),
            )
            if violation_id is None:
                logger.info(
                    "Duplicate violation ignored for tenant %s trace %s rule %s",
                    verdict["tenant_id"],
                    verdict["trace_id"],
                    verdict["rule"],
                )
                continue

            await log_action(
                agent_name=AGENT_NAME,
                action_name=VIOLATION_ACTION,
                parameters=_audit_parameters(verdict),
                result=(
                    f"Recorded {verdict['policy_status']} / "
                    f"{verdict['verdict_card']} for {verdict['rule']}."
                ),
                policy_status=verdict["policy_status"],
                verdict_card=verdict["verdict_card"],
            )

            # Containment runs only for R1 and R2 and only when the tenant
            # exposes a control URL and a shared secret is configured.
            if verdict["rule"] in CONTAINMENT_RULES:
                await _attempt_containment(
                    violation_id=violation_id,
                    verdict=verdict,
                    control_url=state.get("control_url"),
                )

            new_violations.append({**verdict, "violation_id": violation_id})
            continue

        if await has_governance_audit(
            verdict["tenant_id"],
            verdict["trace_id"],
            COMPLIANT_ACTION,
        ):
            logger.info(
                "Duplicate compliant check ignored for tenant %s trace %s",
                verdict["tenant_id"],
                verdict["trace_id"],
            )
            continue

        await log_action(
            agent_name=AGENT_NAME,
            action_name=COMPLIANT_ACTION,
            parameters=_audit_parameters(verdict),
            result=(
                f"Recorded ALLOWED / YES for trace {verdict['trace_id']}."
            ),
            policy_status="ALLOWED",
            verdict_card="YES",
        )
        compliant_checks += 1

    # Missing policy leaves spans unevaluated so the sweep can retry later.
    if state.get("policy") is not None:
        await mark_evaluated(state["tenant_id"], state["trace_ids"])

    return {
        "new_violations": new_violations,
        "compliant_checks": compliant_checks,
    }


async def notify(state: TelemetryState) -> dict[str, Any]:
    """Send one alert per new violation and record the delivery result."""
    enabled = alerts_enabled()
    recipient = state.get("owner_email")
    sent: list[str] = []
    failed: list[str] = []
    disabled = 0

    for violation in state.get("new_violations", []):
        violation_id = int(violation["violation_id"])
        if not enabled:
            await update_violation_alert(violation_id, "disabled")
            disabled += 1
            continue

        if not recipient:
            await update_violation_alert(
                violation_id,
                "failed",
                "Tenant has no owner email.",
            )
            failed.append(f"violation {violation_id}: tenant has no owner email")
            continue

        try:
            confirmation = await send_governance_alert(
                recipient=recipient,
                tenant_id=violation["tenant_id"],
                application_id=violation.get("application_id"),
                amount=(
                    float(violation["amount"])
                    if violation.get("amount") is not None
                    else None
                ),
                threshold=(
                    float(violation["threshold"])
                    if violation.get("threshold") is not None
                    else None
                ),
                currency=violation["currency"],
                rule=violation["rule"],
                reason=violation["reason"],
                policy_status=violation["policy_status"],
                verdict_card=violation["verdict_card"],
            )
        except Exception as exc:
            logger.exception(
                "Governance alert failed for tenant %s trace %s",
                violation["tenant_id"],
                violation["trace_id"],
            )
            await update_violation_alert(
                violation_id,
                "failed",
                str(exc)[:1000],
            )
            failed.append(f"violation {violation_id}: {exc}")
            continue

        await update_violation_alert(violation_id, "sent")
        sent.append(confirmation)

    return {
        "alerts_sent": sent,
        "alerts_failed": failed,
        "alerts_disabled": disabled,
    }


def build_telemetry_monitor_graph():
    """Build and compile the deterministic telemetry StateGraph."""
    graph = StateGraph(TelemetryState)
    graph.add_node("load_spans", load_spans)
    graph.add_node("load_policy", load_policy)
    graph.add_node("evaluate_rules", evaluate_rules)
    graph.add_node("write_verdicts", write_verdicts)
    graph.add_node("notify", notify)

    graph.add_edge(START, "load_spans")
    graph.add_edge("load_spans", "load_policy")
    graph.add_edge("load_policy", "evaluate_rules")
    graph.add_edge("evaluate_rules", "write_verdicts")
    graph.add_edge("write_verdicts", "notify")
    graph.add_edge("notify", END)
    return graph.compile()


telemetry_monitor_graph = build_telemetry_monitor_graph()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _clean_trace_ids(trace_ids: Any) -> list[str]:
    """Validate and de-duplicate trace IDs while preserving order."""
    if isinstance(trace_ids, (str, bytes)) or trace_ids is None:
        raise TypeError("trace_ids must be an iterable of strings.")

    clean_ids: list[str] = []
    for trace_id in trace_ids:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_ids must contain non-empty strings.")
        value = trace_id.strip()
        if value not in clean_ids:
            clean_ids.append(value)
    return clean_ids


async def evaluate_traces(
    tenant_id: str,
    trace_ids: list[str],
) -> dict[str, Any]:
    """Evaluate a batch of traces for one tenant and return a summary.

    Ingestion calls this after storing new spans. The sweep command calls it
    for spans that are still marked evaluated=false. Calling it twice for the
    same trace is safe: violations, compliant audit rows, and alerts are all
    deduplicated.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string.")

    clean_tenant = tenant_id.strip()
    clean_trace_ids = _clean_trace_ids(trace_ids)
    if not clean_trace_ids:
        return {
            "tenant_id": clean_tenant,
            "traces_evaluated": 0,
            "spans_loaded": 0,
            "violations_detected": 0,
            "compliant_checks": 0,
            "alerts_sent": 0,
            "alerts_failed": 0,
            "alerts_disabled": 0,
            "errors": [],
        }

    result = await telemetry_monitor_graph.ainvoke(
        {
            "tenant_id": clean_tenant,
            "trace_ids": clean_trace_ids,
            "spans": [],
            "policy": None,
            "owner_email": None,
            "control_url": None,
            "verdicts": [],
            "new_violations": [],
            "compliant_checks": 0,
            "alerts_sent": [],
            "alerts_failed": [],
            "alerts_disabled": 0,
            "errors": [],
        }
    )

    summary = {
        "tenant_id": clean_tenant,
        "traces_evaluated": len(clean_trace_ids),
        "spans_loaded": len(result.get("spans", [])),
        "violations_detected": len(result.get("new_violations", [])),
        "compliant_checks": int(result.get("compliant_checks", 0)),
        "alerts_sent": len(result.get("alerts_sent", [])),
        "alerts_failed": len(result.get("alerts_failed", [])),
        "alerts_disabled": int(result.get("alerts_disabled", 0)),
        "errors": list(result.get("errors", [])),
    }
    logger.info(
        "Evaluated %d trace(s) for tenant %s: %d violation(s), "
        "%d compliant check(s), %d alert(s) sent, %d disabled.",
        summary["traces_evaluated"],
        summary["tenant_id"],
        summary["violations_detected"],
        summary["compliant_checks"],
        summary["alerts_sent"],
        summary["alerts_disabled"],
    )
    return summary


async def sweep_unevaluated() -> dict[str, Any]:
    """Evaluate every span still marked evaluated=false."""
    tenants = await get_tenants_with_unevaluated_spans()
    totals = {
        "tenants": 0,
        "traces_evaluated": 0,
        "violations_detected": 0,
        "compliant_checks": 0,
        "alerts_sent": 0,
        "alerts_failed": 0,
        "alerts_disabled": 0,
        "errors": [],
    }

    for tenant_id in tenants:
        trace_ids = await get_unevaluated_trace_ids(tenant_id)
        if not trace_ids:
            continue
        totals["tenants"] += 1
        summary = await evaluate_traces(tenant_id, trace_ids)
        totals["traces_evaluated"] += summary["traces_evaluated"]
        totals["violations_detected"] += summary["violations_detected"]
        totals["compliant_checks"] += summary["compliant_checks"]
        totals["alerts_sent"] += summary["alerts_sent"]
        totals["alerts_failed"] += summary["alerts_failed"]
        totals["alerts_disabled"] += summary["alerts_disabled"]
        totals["errors"].extend(
            f"{tenant_id}: {error}" for error in summary["errors"]
        )

    return totals


def main() -> int:
    """Run the recovery sweep command line interface."""
    parser = argparse.ArgumentParser(
        description="TruvanAI deterministic telemetry monitor.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Evaluate every span that is still marked evaluated=false.",
    )
    args = parser.parse_args()

    if not args.sweep:
        parser.print_help()
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    totals = asyncio.run(sweep_unevaluated())
    print(
        "Sweep complete: "
        f"{totals['tenants']} tenant(s), "
        f"{totals['traces_evaluated']} trace(s), "
        f"{totals['violations_detected']} violation(s), "
        f"{totals['compliant_checks']} compliant check(s), "
        f"{totals['alerts_sent']} alert(s) sent, "
        f"{totals['alerts_failed']} alert(s) failed, "
        f"{totals['alerts_disabled']} alert(s) disabled."
    )
    for error in totals["errors"]:
        print(f"  error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
