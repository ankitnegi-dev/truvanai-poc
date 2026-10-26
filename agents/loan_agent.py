"""LangGraph loan approval agent for TruvanAI.

The graph applies the loan approval policy deterministically. A preflight gate
first verifies the opaque request token, loads the trusted amount from
PostgreSQL, and rejects unknown, expired, replayed, flagged, or invalid
requests. Only allowed requests reach the amount router, auto-approval, or the
Redis HITL queue. Groq is configured with the same tools for tool-calling use
cases; policy routing remains deterministic.
"""

from __future__ import annotations

import operator
import os
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph

from shared.loan_store import (
    LoanAuthorization,
    LoanRequestDenied,
    authorize_loan_action,
    record_denial,
)
from shared.policy import CHECK_LOAN_ACTION, HUMAN_APPROVAL_THRESHOLD
from tools.loan_tools import (
    auto_approve_loan,
    check_loan_amount,
    request_human_approval,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

AGENT_NAME = "loan_agent"
model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")


class AgentState(TypedDict):
    """State shared by the loan approval graph nodes."""

    application_id: str
    request_token: str
    amount: float | None
    status: str
    human_required: bool
    blocked_reason: str | None
    audit_entries: Annotated[list[dict[str, Any]], operator.add]


def _build_groq_model(model_name: str) -> ChatGroq:
    """Create a ChatGroq model using the configured Groq API key."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add a Groq API key to .env."
        )
    return ChatGroq(
        model=model_name,
        api_key=api_key,
        temperature=0,
        max_retries=2,
    )


def build_llm_with_tools() -> Any:
    """Bind the loan tools to the primary model with a fallback model."""
    llm = _build_groq_model(model)
    fallback_llm = _build_groq_model(fallback_model)
    tools = [
        check_loan_amount,
        auto_approve_loan,
        request_human_approval,
    ]
    llm_with_tools = llm.bind_tools(tools)
    fallback_with_tools = fallback_llm.bind_tools(tools)
    return llm_with_tools.with_fallbacks([fallback_with_tools])


def _blocked_update(
    decision: LoanAuthorization,
    node: str,
    tool: str,
) -> dict[str, Any]:
    """Build a blocked graph update with one readable audit entry."""
    return {
        "amount": decision.amount,
        "status": "blocked",
        "human_required": False,
        "blocked_reason": decision.reason or "blocked",
        "audit_entries": [
            {
                "node": node,
                "tool": tool,
                "result": {
                    "message": decision.message,
                    "reason": decision.reason,
                },
            }
        ],
    }


async def preflight_gate(state: AgentState) -> dict[str, Any]:
    """Verify the request before any loan state change or queue side effect."""
    decision = await authorize_loan_action(
        state["application_id"],
        state["request_token"],
        CHECK_LOAN_ACTION,
    )
    if not decision.allowed:
        await record_denial(decision, agent_name=AGENT_NAME)
        return _blocked_update(
            decision,
            node="preflight_gate",
            tool="preflight_gate",
        )

    return {
        "amount": decision.amount,
        "status": "verified",
        "blocked_reason": None,
    }


def gate_router(
    state: AgentState,
) -> Literal["loan_checker", "blocked_node"]:
    """Route a verified request to the checker and a denied one to blocked."""
    if state["status"] == "blocked":
        return "blocked_node"
    return "loan_checker"


async def loan_checker(state: AgentState) -> dict[str, Any]:
    """Run the loan amount check and update the graph state."""
    try:
        check_result = await check_loan_amount.ainvoke(
            {
                "application_id": state["application_id"],
                "request_token": state["request_token"],
            }
        )
    except LoanRequestDenied as exc:
        return _blocked_update(
            exc.decision,
            node="loan_checker",
            tool="check_loan_amount",
        )

    if not isinstance(check_result, dict):
        raise TypeError("check_loan_amount returned an unexpected result.")

    return {
        "amount": check_result["amount"],
        "human_required": check_result["human_required"],
        "status": "checked",
        "audit_entries": [
            {
                "node": "loan_checker",
                "tool": "check_loan_amount",
                "result": check_result,
            }
        ],
    }


def router(
    state: AgentState,
) -> Literal["auto_approver", "hitl_requester", "blocked_node"]:
    """Route the application by trusted amount after the policy check."""
    if state["status"] == "blocked":
        return "blocked_node"
    amount = state["amount"]
    if amount is None:
        return "blocked_node"
    if amount <= HUMAN_APPROVAL_THRESHOLD:
        return "auto_approver"
    return "hitl_requester"


async def auto_approver(state: AgentState) -> dict[str, Any]:
    """Run the automatic approval tool for a low-value loan."""
    try:
        confirmation = await auto_approve_loan.ainvoke(
            {
                "application_id": state["application_id"],
                "request_token": state["request_token"],
            }
        )
    except LoanRequestDenied as exc:
        return _blocked_update(
            exc.decision,
            node="auto_approver",
            tool="auto_approve_loan",
        )

    return {
        "status": "approved",
        "human_required": False,
        "audit_entries": [
            {
                "node": "auto_approver",
                "tool": "auto_approve_loan",
                "result": confirmation,
            }
        ],
    }


async def hitl_requester(state: AgentState) -> dict[str, Any]:
    """Queue a high-value loan for human approval."""
    try:
        confirmation = await request_human_approval.ainvoke(
            {
                "application_id": state["application_id"],
                "request_token": state["request_token"],
            }
        )
    except LoanRequestDenied as exc:
        return _blocked_update(
            exc.decision,
            node="hitl_requester",
            tool="request_human_approval",
        )

    return {
        "status": "pending",
        "human_required": True,
        "audit_entries": [
            {
                "node": "hitl_requester",
                "tool": "request_human_approval",
                "result": confirmation,
            }
        ],
    }


async def blocked_node(state: AgentState) -> dict[str, Any]:
    """Terminate a denied request without any side effect."""
    return {
        "status": "blocked",
        "human_required": False,
        "blocked_reason": state.get("blocked_reason") or "blocked",
    }


def build_loan_graph():
    """Build and compile the loan approval StateGraph."""
    graph = StateGraph(AgentState)
    graph.add_node("preflight_gate", preflight_gate)
    graph.add_node("loan_checker", loan_checker)
    graph.add_node("auto_approver", auto_approver)
    graph.add_node("hitl_requester", hitl_requester)
    graph.add_node("blocked_node", blocked_node)

    graph.add_edge(START, "preflight_gate")
    graph.add_conditional_edges(
        "preflight_gate",
        gate_router,
        {
            "loan_checker": "loan_checker",
            "blocked_node": "blocked_node",
        },
    )
    graph.add_conditional_edges(
        "loan_checker",
        router,
        {
            "auto_approver": "auto_approver",
            "hitl_requester": "hitl_requester",
            "blocked_node": "blocked_node",
        },
    )
    graph.add_edge("auto_approver", END)
    graph.add_edge("hitl_requester", END)
    graph.add_edge("blocked_node", END)
    return graph.compile()


loan_graph = build_loan_graph()


def initial_state(application_id: str, request_token: str) -> AgentState:
    """Create a complete initial state for a loan application."""
    return {
        "application_id": application_id,
        "request_token": request_token,
        "amount": None,
        "status": "received",
        "human_required": False,
        "blocked_reason": None,
        "audit_entries": [],
    }


async def run_loan_application(
    application_id: str,
    request_token: str,
) -> AgentState:
    """Run one loan application through the compiled graph."""
    result = await loan_graph.ainvoke(
        initial_state(application_id, request_token)
    )
    return result
