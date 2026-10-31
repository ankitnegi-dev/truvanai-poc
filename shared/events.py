"""External agent event intake for the TruvanAI webhook integration.

A bank's agent can report an action to ``POST /api/events`` without sharing
its source code or database. The event is written to the same governance audit
trail used by the internal agents, the monitor applies the existing policy
rules, and the caller receives the formal verdict synchronously.

The webhook is Approach 2 in ``notes/governance_monitoring_approaches.md``:
one authenticated HTTP call per significant action, with no SDK and no access
to the bank's infrastructure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from agents.monitor_agent import run_monitor
from shared.db import log_action
from shared.loan_store import normalize_application_id
from shared.policy import AUTO_APPROVE_ACTION, MAX_LOAN_AMOUNT

WEBHOOK_SOURCE = "external_webhook"
MAX_AGENT_NAME_LENGTH = 120
MAX_ACTION_NAME_LENGTH = 120
MAX_PARAMETERS_JSON_LENGTH = 10_000
_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal(str(MAX_LOAN_AMOUNT))
_RESERVED_PARAMETER_KEYS = frozenset(
    {"source", "event_timestamp", "application_id", "amount"}
)


class ExternalEventError(ValueError):
    """Raised when an external agent event fails validation."""


@dataclass(frozen=True)
class RecordedExternalEvent:
    """A validated external event stored in the governance audit trail."""

    event_id: int
    agent_name: str
    action_name: str
    application_id: str | None
    amount: float | None
    event_timestamp: datetime


def _clean_text(value: str, field_name: str, max_length: int) -> str:
    """Validate and trim a required event string."""
    if not isinstance(value, str):
        raise ExternalEventError(f"{field_name} must be a string.")
    clean_value = value.strip()
    if not clean_value:
        raise ExternalEventError(f"{field_name} must be a non-empty string.")
    if len(clean_value) > max_length:
        raise ExternalEventError(
            f"{field_name} cannot exceed {max_length} characters."
        )
    return clean_value


def _clean_amount(amount: Any) -> float | None:
    """Validate an optional event amount and return it as a float."""
    if amount is None:
        return None
    if isinstance(amount, bool):
        raise ExternalEventError("amount must be a number.")
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, (int, float, str)):
        text = str(amount).strip()
        if not text:
            raise ExternalEventError("amount must not be empty.")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ExternalEventError("amount must be a number.") from exc
    else:
        raise ExternalEventError("amount must be a number.")

    if not value.is_finite():
        raise ExternalEventError("amount must be finite.")
    if value < 0:
        raise ExternalEventError("amount cannot be negative.")
    if value > _MAX_AMOUNT:
        raise ExternalEventError(
            f"amount cannot exceed {MAX_LOAN_AMOUNT:,.2f}."
        )
    try:
        quantized = value.quantize(_CENT)
    except InvalidOperation as exc:
        raise ExternalEventError(
            "amount must have at most two decimal places."
        ) from exc
    if quantized != value:
        raise ExternalEventError(
            "amount must have at most two decimal places."
        )
    return float(quantized)


def _clean_timestamp(value: datetime | None) -> datetime:
    """Return a timezone-aware UTC timestamp, defaulting to now."""
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ExternalEventError("timestamp must be an ISO-8601 datetime.")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_parameters(
    parameters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate optional client metadata and remove reserved keys."""
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise ExternalEventError("parameters must be a JSON object.")
    clean_parameters = dict(parameters)
    for reserved_key in _RESERVED_PARAMETER_KEYS:
        clean_parameters.pop(reserved_key, None)
    try:
        serialized = json.dumps(clean_parameters, default=str)
    except (TypeError, ValueError) as exc:
        raise ExternalEventError(
            "parameters must be JSON serializable."
        ) from exc
    if len(serialized) > MAX_PARAMETERS_JSON_LENGTH:
        raise ExternalEventError(
            "parameters cannot exceed "
            f"{MAX_PARAMETERS_JSON_LENGTH} serialized characters."
        )
    return clean_parameters


async def record_external_event(
    agent_name: str,
    action_name: str,
    application_id: str | None = None,
    amount: Any = None,
    event_timestamp: datetime | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> RecordedExternalEvent:
    """Validate and persist one external agent event.

    The event is written as ``ALLOWED / YES`` because it records the external
    agent's claim that the action completed. The monitor then changes the row
    to ``BLOCKED / NO`` when a policy rule matches, exactly as it does for an
    action written by one of the internal agents.
    """
    clean_agent = _clean_text(
        agent_name,
        "agent",
        MAX_AGENT_NAME_LENGTH,
    )
    clean_action = _clean_text(
        action_name,
        "action",
        MAX_ACTION_NAME_LENGTH,
    )
    clean_amount = _clean_amount(amount)
    clean_timestamp = _clean_timestamp(event_timestamp)
    clean_parameters = _clean_parameters(parameters)

    clean_application_id: str | None = None
    if application_id is not None:
        try:
            clean_application_id = normalize_application_id(application_id)
        except (TypeError, ValueError) as exc:
            raise ExternalEventError(str(exc)) from exc

    if clean_action == AUTO_APPROVE_ACTION and clean_application_id is None:
        raise ExternalEventError(
            "application_id is required for auto_approve_loan events."
        )

    event_parameters: dict[str, Any] = dict(clean_parameters)
    event_parameters.update(
        {
            "source": WEBHOOK_SOURCE,
            "event_timestamp": clean_timestamp.isoformat(),
        }
    )
    if clean_application_id is not None:
        event_parameters["application_id"] = clean_application_id
    if clean_amount is not None:
        event_parameters["amount"] = clean_amount

    event_id = await log_action(
        agent_name=clean_agent,
        action_name=clean_action,
        parameters=event_parameters,
        result=(
            f"External agent event received: {clean_agent} reported "
            f"{clean_action}."
        ),
        policy_status="ALLOWED",
        verdict_card="YES",
    )
    return RecordedExternalEvent(
        event_id=event_id,
        agent_name=clean_agent,
        action_name=clean_action,
        application_id=clean_application_id,
        amount=clean_amount,
        event_timestamp=clean_timestamp,
    )


async def evaluate_external_event(
    event: RecordedExternalEvent,
) -> dict[str, Any]:
    """Run the monitor against one recorded event and return its verdict."""
    if not isinstance(event, RecordedExternalEvent):
        raise TypeError("event must be a RecordedExternalEvent.")

    monitor_result = await run_monitor(action_ids=[event.event_id])
    blocked = event.event_id in monitor_result["actions_blocked"]
    rules_triggered = sorted(
        {
            str(violation.get("rule", ""))
            for violation in monitor_result["violations_found"]
            if violation.get("rule")
        }
    )
    alerts_sent = len(monitor_result["alerts_sent"])
    verdict = "BLOCKED" if blocked else "ALLOWED"
    verdict_card = "NO" if blocked else "YES"
    message = (
        f"Event {event.event_id} was blocked by the monitor agent."
        if blocked
        else f"Event {event.event_id} passed all policy checks."
    )

    return {
        "event_id": event.event_id,
        "agent": event.agent_name,
        "action": event.action_name,
        "application_id": event.application_id,
        "amount": event.amount,
        "verdict": verdict,
        "verdict_card": verdict_card,
        "rules_triggered": rules_triggered,
        "blocked_action_ids": list(monitor_result["actions_blocked"]),
        "alerts_sent": alerts_sent,
        "message": message,
    }
