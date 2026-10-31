"""Trusted loan intake and preflight authorization for TruvanAI.

The agent never supplies the amount used for a loan decision. A trusted intake
function creates a row in ``loan_applications`` and returns an opaque request
token. Only a SHA-256 hash of that token is stored. Every loan tool calls the
preflight gate in this module before it changes state or touches Redis.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from shared.db import LoanApplication, get_db_connection, log_action
from shared.policy import (
    APPROVED,
    AUTO_APPROVE_ACTION,
    AUTO_APPROVED,
    CHECK_LOAN_ACTION,
    GATE_ACTION,
    HUMAN_APPROVAL_THRESHOLD,
    MAX_LOAN_AMOUNT,
    PENDING_HITL,
    REASON_AMOUNT_MISMATCH,
    REASON_AMOUNT_OUT_OF_BAND,
    REASON_APPLICATION_NOT_FOUND,
    REASON_EXPIRED_REQUEST_TOKEN,
    REASON_INVALID_AMOUNT,
    REASON_INVALID_APPLICATION_ID,
    REASON_INVALID_REQUEST_TOKEN,
    REASON_INVALID_STATE,
    REASON_PERSISTENCE_ERROR,
    REASON_RISK_FLAGS_PRESENT,
    REASON_UNKNOWN_ACTION,
    REJECTED,
    REQUEST_HITL_ACTION,
    REQUEST_TOKEN_TTL_SECONDS,
    SOURCE_STATUS_BY_ACTION,
    SUBMITTED,
    SUPPORTED_GATE_ACTIONS,
    TARGET_STATUS_BY_ACTION,
)

_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_CENT = Decimal("0.01")
_THRESHOLD = Decimal(str(HUMAN_APPROVAL_THRESHOLD))
_MAX_AMOUNT = Decimal(str(MAX_LOAN_AMOUNT))
_MAX_RISK_FLAGS = 20
_MAX_RISK_FLAG_LENGTH = 80
_MAX_TOKEN_LENGTH = 512


@dataclass(frozen=True)
class IssuedLoanRequest:
    """A newly issued trusted loan request and its one-time raw token."""

    application_id: str
    customer_id: str
    amount: float
    status: str
    request_expires_at: datetime
    request_token: str = field(repr=False)


@dataclass(frozen=True)
class LoanAuthorization:
    """The deterministic result of a preflight authorization check."""

    allowed: bool
    action: str
    application_id: str
    reason: str | None
    message: str
    amount: float | None = None
    customer_id: str | None = None
    status: str | None = None
    risk_flags: tuple[str, ...] = ()
    claimed_amount: float | None = None


class LoanRequestDenied(RuntimeError):
    """Raised after a denied loan action has been written to the audit trail."""

    def __init__(self, decision: LoanAuthorization) -> None:
        super().__init__(decision.message)
        self.decision = decision


def _clean_identifier(value: str, field_name: str) -> str:
    """Validate an application or customer identifier."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    clean_value = value.strip()
    if not clean_value:
        raise ValueError(f"{field_name} must be a non-empty string.")
    if not _APPLICATION_ID_PATTERN.fullmatch(clean_value):
        raise ValueError(
            f"{field_name} may contain only letters, numbers, dots, "
            "underscores, and hyphens, and must start with a letter or number."
        )
    return clean_value


def normalize_application_id(application_id: str) -> str:
    """Validate and return a loan application identifier.

    External integrations use the same identifier rules as trusted intake so
    webhook events cannot introduce malformed application IDs into the audit
    trail or the monitor's correlation logic.
    """
    return _clean_identifier(application_id, "application_id")


def _as_decimal(amount: Any) -> Decimal:
    """Convert a loan amount to a validated two-decimal value."""
    if isinstance(amount, bool) or amount is None:
        raise TypeError("amount must be a number.")
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, (int, float, str)):
        text = str(amount).strip()
        if not text:
            raise ValueError("amount must not be empty.")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("amount must be a number.") from exc
    else:
        raise TypeError("amount must be a number.")

    if not value.is_finite():
        raise ValueError("amount must be finite.")
    if value < 0:
        raise ValueError("amount cannot be negative.")
    if value > _MAX_AMOUNT:
        raise ValueError(f"amount cannot exceed {MAX_LOAN_AMOUNT:.2f}.")

    quantized = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if quantized > _MAX_AMOUNT:
        raise ValueError(f"amount cannot exceed {MAX_LOAN_AMOUNT:.2f}.")
    return quantized


def _risk_flags(flags: Iterable[str] | None) -> list[str]:
    """Normalize a risk flag collection and remove duplicates."""
    if flags is None:
        return []
    if isinstance(flags, str):
        candidates: Iterable[Any] = [flags]
    else:
        candidates = flags

    clean_flags: list[str] = []
    for flag in candidates:
        if not isinstance(flag, str):
            raise TypeError("risk_flags must contain only strings.")
        clean_flag = flag.strip()
        if not clean_flag:
            raise ValueError("risk_flags cannot contain empty values.")
        if len(clean_flag) > _MAX_RISK_FLAG_LENGTH:
            raise ValueError(
                f"risk flags cannot exceed {_MAX_RISK_FLAG_LENGTH} characters."
            )
        if clean_flag not in clean_flags:
            clean_flags.append(clean_flag)

    if len(clean_flags) > _MAX_RISK_FLAGS:
        raise ValueError(f"risk_flags cannot exceed {_MAX_RISK_FLAGS} values.")
    return clean_flags


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_request_token(request_token: str) -> str:
    """Return the SHA-256 hash stored for a raw request token."""
    if not isinstance(request_token, str) or not request_token.strip():
        raise ValueError("request_token must be a non-empty string.")
    if len(request_token) > _MAX_TOKEN_LENGTH:
        raise ValueError("request_token is too long.")
    return hashlib.sha256(request_token.encode("utf-8")).hexdigest()


def _safe_amount(value: Any) -> float | None:
    """Return a finite float for display or None when the value is invalid."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _denial(
    action: str,
    application_id: str,
    reason: str,
    message: str,
    *,
    amount: float | None = None,
    customer_id: str | None = None,
    status: str | None = None,
    risk_flags: Iterable[str] = (),
    claimed_amount: float | None = None,
) -> LoanAuthorization:
    """Build a denied authorization result."""
    return LoanAuthorization(
        allowed=False,
        action=action,
        application_id=application_id,
        reason=reason,
        message=message,
        amount=amount,
        customer_id=customer_id,
        status=status,
        risk_flags=tuple(risk_flags),
        claimed_amount=claimed_amount,
    )


async def issue_loan_request(
    application_id: str,
    customer_id: str,
    amount: float,
    risk_flags: Iterable[str] | None = None,
    ttl_seconds: int = REQUEST_TOKEN_TTL_SECONDS,
) -> IssuedLoanRequest:
    """Create a trusted loan application and return its opaque request token.

    This function models a trusted intake service. It must never be callable
    from an agent tool. The raw token is returned once and only its hash is
    persisted.
    """
    clean_application_id = _clean_identifier(application_id, "application_id")
    clean_customer_id = _clean_identifier(customer_id, "customer_id")
    clean_amount = _as_decimal(amount)
    clean_flags = _risk_flags(risk_flags)

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TypeError("ttl_seconds must be an integer.")
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds cannot be negative.")

    request_token = secrets.token_urlsafe(32)
    token_hash = hash_request_token(request_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    entry = LoanApplication(
        application_id=clean_application_id,
        customer_id=clean_customer_id,
        amount=clean_amount,
        status=SUBMITTED,
        request_token_hash=token_hash,
        request_expires_at=expires_at,
        risk_flags=clean_flags,
    )

    async with get_db_connection() as session:
        session.add(entry)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(
                f"Loan application {clean_application_id} already exists."
            ) from exc

    return IssuedLoanRequest(
        application_id=clean_application_id,
        customer_id=clean_customer_id,
        amount=float(clean_amount),
        status=SUBMITTED,
        request_expires_at=expires_at,
        request_token=request_token,
    )


async def get_loan_application(
    application_id: str,
) -> dict[str, Any] | None:
    """Return safe trusted loan fields without the request token hash."""
    clean_application_id = _clean_identifier(application_id, "application_id")
    statement = select(LoanApplication).where(
        LoanApplication.application_id == clean_application_id
    )
    async with get_db_connection() as session:
        row = await session.scalar(statement)

    if row is None:
        return None

    return {
        "application_id": row.application_id,
        "customer_id": row.customer_id,
        "amount": float(row.amount),
        "status": row.status,
        "risk_flags": list(row.risk_flags or []),
        "request_expires_at": _as_utc(row.request_expires_at).isoformat(),
        "created_at": _as_utc(row.created_at).isoformat(),
        "updated_at": _as_utc(row.updated_at).isoformat(),
    }


async def get_recent_auto_approved_loans(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent automatic approvals for dashboard display."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 1:
        raise ValueError("limit must be positive.")
    clean_limit = min(limit, 50)

    statement = (
        select(LoanApplication)
        .where(LoanApplication.status == AUTO_APPROVED)
        .order_by(
            LoanApplication.updated_at.desc(),
            LoanApplication.id.desc(),
        )
        .limit(clean_limit)
    )
    async with get_db_connection() as session:
        rows = (await session.scalars(statement)).all()

    return [
        {
            "application_id": row.application_id,
            "customer_id": row.customer_id,
            "amount": float(row.amount),
            "status": "approved",
            "requested_at": _as_utc(row.created_at).isoformat(),
            "resolved_at": _as_utc(row.updated_at).isoformat(),
            "agent_name": "loan_agent",
            "resolution": "automatic",
        }
        for row in rows
    ]


async def authorize_loan_action(
    application_id: str,
    request_token: str,
    action: str,
    claimed_amount: float | None = None,
) -> LoanAuthorization:
    """Apply every preflight rule before a loan tool may act.

    Checks are deterministic: identifier format, token hash, token expiry,
    risk flags, trusted amount, optional claimed amount, application state,
    and the amount band for the requested action.
    """
    if action not in SUPPORTED_GATE_ACTIONS:
        return _denial(
            str(action),
            str(application_id),
            REASON_UNKNOWN_ACTION,
            "The requested loan action is not recognized.",
        )

    try:
        clean_application_id = _clean_identifier(
            application_id,
            "application_id",
        )
    except (TypeError, ValueError):
        return _denial(
            action,
            str(application_id),
            REASON_INVALID_APPLICATION_ID,
            "The application identifier is invalid.",
        )

    if (
        not isinstance(request_token, str)
        or not request_token.strip()
        or len(request_token) > _MAX_TOKEN_LENGTH
    ):
        return _denial(
            action,
            clean_application_id,
            REASON_INVALID_REQUEST_TOKEN,
            "The request token is not valid for this application.",
        )

    statement = select(LoanApplication).where(
        LoanApplication.application_id == clean_application_id
    )
    async with get_db_connection() as session:
        row = await session.scalar(statement)

    if row is None:
        return _denial(
            action,
            clean_application_id,
            REASON_APPLICATION_NOT_FOUND,
            "The application was not found in the trusted loan record.",
        )

    supplied_hash = hash_request_token(request_token)
    if not hmac.compare_digest(supplied_hash, row.request_token_hash):
        return _denial(
            action,
            clean_application_id,
            REASON_INVALID_REQUEST_TOKEN,
            "The request token is not valid for this application.",
        )

    if _as_utc(row.request_expires_at) < datetime.now(timezone.utc):
        return _denial(
            action,
            clean_application_id,
            REASON_EXPIRED_REQUEST_TOKEN,
            "The request token has expired.",
        )

    clean_flags = tuple(row.risk_flags or [])
    if clean_flags:
        return _denial(
            action,
            clean_application_id,
            REASON_RISK_FLAGS_PRESENT,
            "The application has risk flags that require security review.",
            amount=_safe_amount(row.amount),
            customer_id=row.customer_id,
            status=row.status,
            risk_flags=clean_flags,
        )

    try:
        trusted_amount = _as_decimal(row.amount)
    except (TypeError, ValueError):
        return _denial(
            action,
            clean_application_id,
            REASON_INVALID_AMOUNT,
            "The trusted loan amount is invalid.",
            customer_id=row.customer_id,
            status=row.status,
        )

    claimed_value: Decimal | None = None
    if claimed_amount is not None:
        try:
            claimed_value = _as_decimal(claimed_amount)
        except (TypeError, ValueError):
            return _denial(
                action,
                clean_application_id,
                REASON_INVALID_AMOUNT,
                "The supplied amount is not a valid loan amount.",
                amount=float(trusted_amount),
                customer_id=row.customer_id,
                status=row.status,
            )
        if claimed_value != trusted_amount:
            return _denial(
                action,
                clean_application_id,
                REASON_AMOUNT_MISMATCH,
                "The supplied amount does not match the trusted application record.",
                amount=float(trusted_amount),
                customer_id=row.customer_id,
                status=row.status,
                claimed_amount=float(claimed_value),
            )

    allowed_source_statuses = SOURCE_STATUS_BY_ACTION[action]
    if row.status not in allowed_source_statuses:
        return _denial(
            action,
            clean_application_id,
            REASON_INVALID_STATE,
            f"Application status {row.status} does not allow this action.",
            amount=float(trusted_amount),
            customer_id=row.customer_id,
            status=row.status,
        )

    if action == AUTO_APPROVE_ACTION and trusted_amount > _THRESHOLD:
        return _denial(
            action,
            clean_application_id,
            REASON_AMOUNT_OUT_OF_BAND,
            "Automatic approval is limited to "
            f"{HUMAN_APPROVAL_THRESHOLD:.2f}. The trusted amount is "
            f"{float(trusted_amount):.2f}.",
            amount=float(trusted_amount),
            customer_id=row.customer_id,
            status=row.status,
        )

    if action == REQUEST_HITL_ACTION and trusted_amount <= _THRESHOLD:
        return _denial(
            action,
            clean_application_id,
            REASON_AMOUNT_OUT_OF_BAND,
            "Human approval is only used above "
            f"{HUMAN_APPROVAL_THRESHOLD:.2f}. The trusted amount is "
            f"{float(trusted_amount):.2f}.",
            amount=float(trusted_amount),
            customer_id=row.customer_id,
            status=row.status,
        )

    if action == CHECK_LOAN_ACTION:
        if trusted_amount > _THRESHOLD:
            message = (
                f"Application {clean_application_id} requires human approval "
                f"for {float(trusted_amount):.2f}."
            )
        else:
            message = (
                f"Application {clean_application_id} is within the automatic "
                f"approval limit for {float(trusted_amount):.2f}."
            )
    elif action == AUTO_APPROVE_ACTION:
        message = (
            f"Application {clean_application_id} for "
            f"{float(trusted_amount):.2f} is eligible for automatic approval."
        )
    else:
        message = (
            f"Application {clean_application_id} for "
            f"{float(trusted_amount):.2f} is eligible for human approval review."
        )

    return LoanAuthorization(
        allowed=True,
        action=action,
        application_id=clean_application_id,
        reason=None,
        message=message,
        amount=float(trusted_amount),
        customer_id=row.customer_id,
        status=row.status,
        risk_flags=clean_flags,
        claimed_amount=float(claimed_value) if claimed_value is not None else None,
    )


async def record_denial(
    decision: LoanAuthorization,
    agent_name: str = "loan_agent",
) -> int:
    """Write a blocked preflight result without leaking the raw token."""
    if decision.allowed:
        raise ValueError("Only denied loan decisions can be recorded.")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("agent_name must be a non-empty string.")

    parameters: dict[str, Any] = {
        "application_id": decision.application_id,
        "attempted_action": decision.action,
        "reason": decision.reason,
        "application_status": decision.status,
        "trusted_amount": decision.amount,
        "claimed_amount": decision.claimed_amount,
        "risk_flags": list(decision.risk_flags),
    }

    return await log_action(
        agent_name=agent_name.strip(),
        action_name=GATE_ACTION,
        parameters=parameters,
        result=f"Preflight denied {decision.action}: {decision.message}",
        policy_status="BLOCKED",
        verdict_card="NO",
    )


async def consume_loan_request(
    application_id: str,
    request_token: str,
    action: str,
    claimed_amount: float | None = None,
) -> tuple[LoanAuthorization, bool]:
    """Authorize an action and atomically move the loan to its next state.

    The conditional update is the second half of the gate. It prevents two
    concurrent requests, a replayed token, or a state change from producing a
    duplicate approval or queue entry.
    """
    decision = await authorize_loan_action(
        application_id,
        request_token,
        action,
        claimed_amount=claimed_amount,
    )
    if not decision.allowed:
        return decision, False

    if action not in TARGET_STATUS_BY_ACTION:
        raise ValueError(f"Action {action} does not change loan state.")

    target_status = TARGET_STATUS_BY_ACTION[action]
    try:
        token_hash = hash_request_token(request_token)
        clean_application_id = _clean_identifier(
            application_id,
            "application_id",
        )
        trusted_amount = _as_decimal(decision.amount)
    except (TypeError, ValueError):
        return (
            _denial(
                action,
                str(application_id),
                REASON_PERSISTENCE_ERROR,
                "The loan action could not be applied.",
            ),
            False,
        )

    statement = (
        update(LoanApplication)
        .where(
            LoanApplication.application_id == clean_application_id,
            LoanApplication.status == SUBMITTED,
            LoanApplication.request_token_hash == token_hash,
            LoanApplication.amount == trusted_amount,
            LoanApplication.request_expires_at >= datetime.now(timezone.utc),
        )
        .values(status=target_status)
        .returning(LoanApplication.id)
    )
    if action == AUTO_APPROVE_ACTION:
        statement = statement.where(LoanApplication.amount <= _THRESHOLD)
    else:
        statement = statement.where(LoanApplication.amount > _THRESHOLD)

    async with get_db_connection() as session:
        updated_id = await session.scalar(statement)
        if updated_id is None:
            await session.rollback()
            return (
                _denial(
                    action,
                    clean_application_id,
                    REASON_INVALID_STATE,
                    "The application is no longer available for this action.",
                    amount=decision.amount,
                    customer_id=decision.customer_id,
                    status=decision.status,
                ),
                False,
            )
        await session.commit()

    return decision, True


async def rollback_loan_transition(
    application_id: str,
    request_token: str,
    expected_status: str,
) -> bool:
    """Restore a loan to SUBMITTED after a failed side effect."""
    clean_application_id = _clean_identifier(application_id, "application_id")
    token_hash = hash_request_token(request_token)
    statement = (
        update(LoanApplication)
        .where(
            LoanApplication.application_id == clean_application_id,
            LoanApplication.status == expected_status,
            LoanApplication.request_token_hash == token_hash,
        )
        .values(status=SUBMITTED)
        .returning(LoanApplication.id)
    )
    async with get_db_connection() as session:
        restored_id = await session.scalar(statement)
        if restored_id is None:
            await session.rollback()
            return False
        await session.commit()
    return True


async def resolve_hitl_loan(application_id: str, decision: str) -> bool:
    """Move a pending trusted loan to approved or rejected."""
    clean_application_id = _clean_identifier(application_id, "application_id")
    if decision == "approve":
        target_status = APPROVED
    elif decision == "reject":
        target_status = REJECTED
    else:
        raise ValueError(f"Unsupported decision: {decision}")

    statement = (
        update(LoanApplication)
        .where(
            LoanApplication.application_id == clean_application_id,
            LoanApplication.status == PENDING_HITL,
        )
        .values(status=target_status)
        .returning(LoanApplication.id)
    )
    async with get_db_connection() as session:
        updated_id = await session.scalar(statement)
        if updated_id is None:
            await session.rollback()
            return False
        await session.commit()
    return True


async def restore_hitl_loan(application_id: str) -> bool:
    """Restore a resolved trusted loan to PENDING_HITL after a failed audit."""
    clean_application_id = _clean_identifier(application_id, "application_id")
    statement = (
        update(LoanApplication)
        .where(
            LoanApplication.application_id == clean_application_id,
            LoanApplication.status.in_((APPROVED, REJECTED)),
        )
        .values(status=PENDING_HITL)
        .returning(LoanApplication.id)
    )
    async with get_db_connection() as session:
        restored_id = await session.scalar(statement)
        if restored_id is None:
            await session.rollback()
            return False
        await session.commit()
    return True


async def delete_loan_application(application_id: str) -> bool:
    """Delete a trusted loan record for demo setup or test cleanup."""
    clean_application_id = _clean_identifier(application_id, "application_id")
    statement = (
        delete(LoanApplication)
        .where(LoanApplication.application_id == clean_application_id)
        .returning(LoanApplication.id)
    )
    async with get_db_connection() as session:
        deleted_id = await session.scalar(statement)
        if deleted_id is None:
            await session.rollback()
            return False
        await session.commit()
    return True
