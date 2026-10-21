"""Real service tools used by the loan approval agent.

Each tool writes its action to PostgreSQL through the async audit helper.
Human approval requests are persisted in the Redis queue consumed by the
FastAPI HITL dashboard. No in-memory fallback is provided.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.tools import tool
from redis.asyncio import Redis

from shared.db import log_action

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

AGENT_NAME = "loan_agent"
HITL_QUEUE = "hitl:loan_approvals"
HUMAN_APPROVAL_THRESHOLD = 1000.0

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


def _validated_application_id(application_id: str) -> str:
    """Validate and normalize an application identifier."""
    if not isinstance(application_id, str) or not application_id.strip():
        raise ValueError("application_id must be a non-empty string.")
    return application_id.strip()


def _validated_amount(amount: float) -> float:
    """Validate a loan amount and return it as a finite float."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise TypeError("amount must be a number.")
    value = float(amount)
    if not math.isfinite(value):
        raise ValueError("amount must be finite.")
    if value < 0:
        raise ValueError("amount cannot be negative.")
    return value


def _human_approval_required(amount: float) -> bool:
    """Apply the loan policy threshold to an amount."""
    return amount > HUMAN_APPROVAL_THRESHOLD


@tool
async def check_loan_amount(
    application_id: str,
    amount: float,
) -> dict[str, Any]:
    """Check a loan amount and flag whether human approval is required."""
    clean_application_id = _validated_application_id(application_id)
    clean_amount = _validated_amount(amount)
    human_required = _human_approval_required(clean_amount)

    if human_required:
        message = (
            f"Application {clean_application_id} requires human approval "
            f"for {clean_amount:.2f}."
        )
        result = (
            f"Checked amount {clean_amount:.2f} for application "
            f"{clean_application_id}. Human approval is required."
        )
    else:
        message = (
            f"Application {clean_application_id} is within the automatic "
            f"approval limit for {clean_amount:.2f}."
        )
        result = (
            f"Checked amount {clean_amount:.2f} for application "
            f"{clean_application_id}. Automatic approval is allowed."
        )

    await log_action(
        agent_name=AGENT_NAME,
        action_name="check_loan_amount",
        parameters={
            "application_id": clean_application_id,
            "amount": clean_amount,
        },
        result=result,
        policy_status="ALLOWED",
        verdict_card="YES",
    )

    return {
        "application_id": clean_application_id,
        "amount": clean_amount,
        "human_required": human_required,
        "message": message,
    }


@tool
async def auto_approve_loan(application_id: str) -> str:
    """Mark a loan application as automatically approved."""
    clean_application_id = _validated_application_id(application_id)
    confirmation = (
        f"Loan application {clean_application_id} has been approved."
    )

    await log_action(
        agent_name=AGENT_NAME,
        action_name="auto_approve_loan",
        parameters={"application_id": clean_application_id},
        result=confirmation,
        policy_status="ALLOWED",
        verdict_card="YES",
    )
    return confirmation


@tool
async def request_human_approval(application_id: str, amount: float) -> str:
    """Queue a loan application for human approval through Redis."""
    clean_application_id = _validated_application_id(application_id)
    clean_amount = _validated_amount(amount)
    requested_at = datetime.now(timezone.utc).isoformat()

    payload = {
        "application_id": clean_application_id,
        "amount": clean_amount,
        "status": "pending",
        "requested_at": requested_at,
    }

    client = get_redis_client()
    await client.rpush(HITL_QUEUE, json.dumps(payload))

    confirmation = (
        f"Loan application {clean_application_id} for {clean_amount:.2f} "
        "has been queued for human approval."
    )
    await log_action(
        agent_name=AGENT_NAME,
        action_name="request_human_approval",
        parameters={
            "application_id": clean_application_id,
            "amount": clean_amount,
            "queue": HITL_QUEUE,
            "requested_at": requested_at,
        },
        result=confirmation,
        policy_status="PENDING",
        verdict_card="UNKNOWN",
    )
    return confirmation
