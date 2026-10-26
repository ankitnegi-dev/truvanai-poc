"""Shared loan policy and preflight gate constants.

The values in this module are intentionally neutral so the database layer,
tools, agents, dashboard, and tests can import them without circular imports.
"""

from __future__ import annotations

CHECK_LOAN_ACTION = "check_loan_amount"
AUTO_APPROVE_ACTION = "auto_approve_loan"
REQUEST_HITL_ACTION = "request_human_approval"
GATE_ACTION = "preflight_gate"

HUMAN_APPROVAL_THRESHOLD = 1000.0
MAX_LOAN_AMOUNT = 9_999_999_999.99
REQUEST_TOKEN_TTL_SECONDS = 15 * 60

SUBMITTED = "SUBMITTED"
PENDING_HITL = "PENDING_HITL"
AUTO_APPROVED = "AUTO_APPROVED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"

TERMINAL_LOAN_STATUSES = frozenset({AUTO_APPROVED, APPROVED, REJECTED, BLOCKED})
SUPPORTED_GATE_ACTIONS = frozenset(
    {CHECK_LOAN_ACTION, AUTO_APPROVE_ACTION, REQUEST_HITL_ACTION}
)
SOURCE_STATUS_BY_ACTION = {
    CHECK_LOAN_ACTION: frozenset({SUBMITTED}),
    AUTO_APPROVE_ACTION: frozenset({SUBMITTED}),
    REQUEST_HITL_ACTION: frozenset({SUBMITTED}),
}
TARGET_STATUS_BY_ACTION = {
    AUTO_APPROVE_ACTION: AUTO_APPROVED,
    REQUEST_HITL_ACTION: PENDING_HITL,
}

REASON_INVALID_APPLICATION_ID = "invalid_application_id"
REASON_APPLICATION_NOT_FOUND = "unknown_application"
REASON_INVALID_REQUEST_TOKEN = "invalid_request_token"
REASON_EXPIRED_REQUEST_TOKEN = "expired_request_token"
REASON_RISK_FLAGS_PRESENT = "risk_flags_present"
REASON_INVALID_AMOUNT = "invalid_amount"
REASON_AMOUNT_MISMATCH = "amount_mismatch"
REASON_INVALID_STATE = "invalid_state"
REASON_AMOUNT_OUT_OF_BAND = "amount_out_of_band"
REASON_UNKNOWN_ACTION = "unknown_action"
REASON_PERSISTENCE_ERROR = "persistence_error"

HITL_QUEUE = "hitl:loan_approvals"
HITL_APPROVED_QUEUE = "hitl:loan_approvals:approved"
HITL_REJECTED_QUEUE = "hitl:loan_approvals:rejected"
