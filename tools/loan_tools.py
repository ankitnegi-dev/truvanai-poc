"""Real service tools used by the loan approval agent.

Every tool calls the preflight gate in ``shared.loan_store`` before it changes
a loan record or touches the Redis HITL queue. The amount used for a decision
comes from PostgreSQL, not from the agent. Each action and denial is written to
the permanent audit trail.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from dotenv import load_dotenv
from langchain_core.tools import tool
from redis.asyncio import Redis

from shared.db import log_action
from shared.loan_store import (
    LoanAuthorization,
    LoanRequestDenied,
    authorize_loan_action,
    consume_loan_request,
    record_denial,
    rollback_loan_transition,
)
from shared.policy import (
    AUTO_APPROVE_ACTION,
    AUTO_APPROVED,
    CHECK_LOAN_ACTION,
    HITL_QUEUE,
    HUMAN_APPROVAL_THRESHOLD,
    PENDING_HITL,
    REQUEST_HITL_ACTION,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

AGENT_NAME = "loan_agent"
logger = logging.getLogger("truvanai.loan_tools")

_redis_client: Redis | None = None


def get_redis_client() -> Redis:
    """Return a process-wide async Redis client using REDIS_URL."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not redis_url:
            raise RuntimeError(
                "REDIS_URL is not set. Add a Redis connection string to .env."
            )
        _redis_client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _redis_client


async def close_redis_connection() -> None:
    """Close the shared Redis client and reset the module state."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def _restore_after_side_effect_failure(
    application_id: str,
    request_token: str,
    expected_status: str,
) -> None:
    """Best-effort status restoration after a failed queue or audit write."""
    try:
        restored = await rollback_loan_transition(
            application_id,
            request_token,
            expected_status,
        )
    except Exception:
        logger.exception(
            "Failed to restore application %s after a side effect failure.",
            application_id,
        )
        return
    if not restored:
        logger.error(
            "Application %s was not restored from %s to SUBMITTED.",
            application_id,
            expected_status,
        )


async def _raise_denial(decision: LoanAuthorization) -> NoReturn:
    """Record a denied action and raise the typed denial exception."""
    if decision.allowed:
        raise RuntimeError("A denied action received an allowed decision.")
    await record_denial(decision, agent_name=AGENT_NAME)
    raise LoanRequestDenied(decision)


@tool
async def check_loan_amount(
    application_id: str,
    request_token: str,
    amount: float | None = None,
) -> dict[str, Any]:
    """Check a loan against the trusted application record.

    The trusted amount is loaded from PostgreSQL. If an amount is supplied, it
    is only compared with the stored amount and is never used for the decision.
    """
    decision = await authorize_loan_action(
        application_id,
        request_token,
        CHECK_LOAN_ACTION,
        claimed_amount=amount,
    )
    if not decision.allowed:
        await _raise_denial(decision)

    if decision.amount is None:
        raise RuntimeError("Authorized loan is missing its trusted amount.")

    clean_application_id = decision.application_id
    trusted_amount = decision.amount
    human_required = trusted_amount > HUMAN_APPROVAL_THRESHOLD

    if human_required:
        message = (
            f"Application {clean_application_id} requires human approval "
            f"for {trusted_amount:.2f}."
        )
        result = (
            f"Checked trusted amount {trusted_amount:.2f} for application "
            f"{clean_application_id}. Human approval is required."
        )
    else:
        message = (
            f"Application {clean_application_id} is within the automatic "
            f"approval limit for {trusted_amount:.2f}."
        )
        result = (
            f"Checked trusted amount {trusted_amount:.2f} for application "
            f"{clean_application_id}. Automatic approval is allowed."
        )

    await log_action(
        agent_name=AGENT_NAME,
        action_name=CHECK_LOAN_ACTION,
        parameters={
            "application_id": clean_application_id,
            "amount": trusted_amount,
            "amount_source": "loan_applications",
            "human_required": human_required,
        },
        result=result,
        policy_status="ALLOWED",
        verdict_card="YES",
    )

    return {
        "application_id": clean_application_id,
        "amount": trusted_amount,
        "human_required": human_required,
        "message": message,
    }


@tool
async def auto_approve_loan(application_id: str, request_token: str) -> str:
    """Approve a verified loan at or below the automatic approval limit."""
    decision, applied = await consume_loan_request(
        application_id,
        request_token,
        AUTO_APPROVE_ACTION,
    )
    if not applied:
        if decision.allowed:
            raise RuntimeError(
                "The loan state changed without a denial decision."
            )
        await _raise_denial(decision)

    clean_application_id = decision.application_id
    trusted_amount = decision.amount
    if trusted_amount is None:
        await _restore_after_side_effect_failure(
            clean_application_id,
            request_token,
            AUTO_APPROVED,
        )
        raise RuntimeError("Authorized loan is missing its trusted amount.")

    confirmation = (
        f"Loan application {clean_application_id} has been approved."
    )

    try:
        await log_action(
            agent_name=AGENT_NAME,
            action_name=AUTO_APPROVE_ACTION,
            parameters={
                "application_id": clean_application_id,
                "amount": trusted_amount,
                "amount_source": "loan_applications",
                "status": AUTO_APPROVED,
            },
            result=confirmation,
            policy_status="ALLOWED",
            verdict_card="YES",
        )
    except Exception:
        await _restore_after_side_effect_failure(
            clean_application_id,
            request_token,
            AUTO_APPROVED,
        )
        raise

    return confirmation


@tool
async def request_human_approval(
    application_id: str,
    request_token: str,
) -> str:
    """Queue a verified high-value loan for human approval through Redis."""
    decision, applied = await consume_loan_request(
        application_id,
        request_token,
        REQUEST_HITL_ACTION,
    )
    if not applied:
        if decision.allowed:
            raise RuntimeError(
                "The loan state changed without a denial decision."
            )
        await _raise_denial(decision)

    clean_application_id = decision.application_id
    trusted_amount = decision.amount
    if trusted_amount is None:
        await _restore_after_side_effect_failure(
            clean_application_id,
            request_token,
            PENDING_HITL,
        )
        raise RuntimeError("Authorized loan is missing its trusted amount.")

    requested_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "application_id": clean_application_id,
        "customer_id": decision.customer_id,
        "amount": trusted_amount,
        "amount_source": "loan_applications",
        "status": "pending",
        "requested_at": requested_at,
    }
    raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    try:
        client = get_redis_client()
        await client.rpush(HITL_QUEUE, raw_payload)
    except Exception:
        await _restore_after_side_effect_failure(
            clean_application_id,
            request_token,
            PENDING_HITL,
        )
        raise

    confirmation = (
        f"Loan application {clean_application_id} for {trusted_amount:.2f} "
        "has been queued for human approval."
    )
    try:
        await log_action(
            agent_name=AGENT_NAME,
            action_name=REQUEST_HITL_ACTION,
            parameters={
                "application_id": clean_application_id,
                "amount": trusted_amount,
                "amount_source": "loan_applications",
                "queue": HITL_QUEUE,
                "requested_at": requested_at,
            },
            result=confirmation,
            policy_status="PENDING",
            verdict_card="UNKNOWN",
        )
    except Exception:
        try:
            await client.lrem(HITL_QUEUE, 1, raw_payload)
        except Exception:
            logger.exception(
                "Failed to remove application %s from the HITL queue.",
                clean_application_id,
            )
        await _restore_after_side_effect_failure(
            clean_application_id,
            request_token,
            PENDING_HITL,
        )
        raise

    return confirmation
