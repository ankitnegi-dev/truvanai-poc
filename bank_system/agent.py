# bank_system/agent.py - bank loan agent.
#
# This package plays the role of the CUSTOMER. It does NOT import from
# shared/, tools/, agents/, hitl/, or ingest/.
#
# process_loan emits the following spans (contract defined in docs/OTEL_CONTRACT.md):
#
#   loan.process (root span)
#     loan.application_id  string
#     loan.amount          double
#     loan.currency        "INR"
#     agent.name           "bank_loan_agent"
#
#   agent.tool.check_amount (child)
#     loan.application_id  string
#     loan.amount          double
#
#   agent.tool.auto_approve (child, when amount <= BANK_APPROVAL_THRESHOLD)
#     loan.application_id  string
#     loan.amount          double
#     loan.decision        "AUTO_APPROVED"
#
#   agent.tool.request_human_review (child, when amount > BANK_APPROVAL_THRESHOLD)
#     loan.application_id  string
#     loan.amount          double
#     loan.decision        "SENT_TO_HITL"
#
# Resource: service.name "bank-loan-agent"

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from bank_system.config import BANK_APPROVAL_THRESHOLD
from bank_system.store import get_loan, init_store, insert_loan, update_loan_status
from bank_system.telemetry import get_tracer

CURRENCY = "INR"
AGENT_NAME = "bank_loan_agent"


def process_loan(application_id: str, amount: float) -> dict:
    """Process a loan application and emit OTel spans for every step.

    Returns a dict with application_id, amount, bank_threshold, decision,
    and status suitable for printing in run.py.
    """
    init_store()
    insert_loan(application_id, amount)

    tracer = get_tracer()

    with tracer.start_as_current_span("loan.process") as root_span:
        root_span.set_attribute("loan.application_id", application_id)
        root_span.set_attribute("loan.amount", float(amount))
        root_span.set_attribute("loan.currency", CURRENCY)
        root_span.set_attribute("agent.name", AGENT_NAME)

        # Tool: check_amount
        with tracer.start_as_current_span("agent.tool.check_amount") as check_span:
            check_span.set_attribute("loan.application_id", application_id)
            check_span.set_attribute("loan.amount", float(amount))

        # Route based on the BANK's own threshold (not TruvanAI's policy).
        if amount <= BANK_APPROVAL_THRESHOLD:
            with tracer.start_as_current_span("agent.tool.auto_approve") as approve_span:
                approve_span.set_attribute("loan.application_id", application_id)
                approve_span.set_attribute("loan.amount", float(amount))
                approve_span.set_attribute("loan.decision", "AUTO_APPROVED")
            update_loan_status(application_id, "AUTO_APPROVED")
            decision = "AUTO_APPROVED"
        else:
            with tracer.start_as_current_span(
                "agent.tool.request_human_review"
            ) as review_span:
                review_span.set_attribute("loan.application_id", application_id)
                review_span.set_attribute("loan.amount", float(amount))
                review_span.set_attribute("loan.decision", "SENT_TO_HITL")
            update_loan_status(application_id, "SENT_TO_HITL")
            decision = "SENT_TO_HITL"

        root_span.set_status(StatusCode.OK)

    loan = get_loan(application_id)
    return {
        "application_id": application_id,
        "amount": amount,
        "bank_threshold": BANK_APPROVAL_THRESHOLD,
        "decision": decision,
        "status": loan["status"] if loan else "UNKNOWN",
    }
