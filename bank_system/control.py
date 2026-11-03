"""Bank-side control API for containment callbacks.

This module plays the role of a control surface that a real bank chooses to
expose. TruvanAI calls it after detecting an R1 or R2 violation so the bank
can move an auto-approved loan into a held state for review.

Run with:

    python -m uvicorn bank_system.control:app --port 8090

The endpoint is protected by the shared secret in BANK_CONTROL_SECRET. The
secret must match the value configured in the TruvanAI monitor environment.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from bank_system.store import get_loan, init_store, update_loan_status

init_store()

app = FastAPI(
    title="Bank control API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

CONTROL_SECRET_HEADER = "x-bank-control-secret"


def _expected_secret() -> str:
    return os.getenv("BANK_CONTROL_SECRET", "").strip()


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/control/hold/{application_id}")
async def hold_loan(
    application_id: str,
    x_bank_control_secret: str | None = Header(
        default=None, alias=CONTROL_SECRET_HEADER
    ),
) -> dict[str, Any]:
    """Move an AUTO_APPROVED loan to ON_HOLD_FOR_REVIEW."""
    expected = _expected_secret()
    if (
        not expected
        or not x_bank_control_secret
        or not hmac.compare_digest(x_bank_control_secret, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bank control secret.",
        )

    loan = get_loan(application_id)
    if loan is None:
        raise HTTPException(
            status_code=404,
            detail="Loan not found.",
        )
    if loan["status"] not in ("AUTO_APPROVED", "RECEIVED"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Loan {application_id} is {loan['status']}, "
                "not eligible for hold."
            ),
        )

    update_loan_status(application_id, "ON_HOLD_FOR_REVIEW")
    return {
        "application_id": application_id,
        "status": "ON_HOLD_FOR_REVIEW",
    }
