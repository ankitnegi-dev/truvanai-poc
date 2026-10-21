"""LangGraph loan approval agent for TruvanAI.

The graph applies the loan approval policy deterministically. It first checks
the requested amount, then routes low-value loans to automatic approval and
high-value loans to the human approval queue. Groq is configured with the
same tools for tool-calling use cases; policy routing remains deterministic
so every execution follows the same governance rules.
"""

from __future__ import annotations

import operator
import os
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph

from tools.loan_tools import (
    HUMAN_APPROVAL_THRESHOLD,
    auto_approve_loan,
    check_loan_amount,
    request_human_approval,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")


class AgentState(TypedDict):
    """State shared by the loan approval graph nodes."""

    application_id: str
    amount: float
    status: str
    human_required: bool
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
    llm_with_tools = llm.bind_tools(
        [
            check_loan_amount,
            auto_approve_loan,
            request_human_approval,
        ]
    )
    fallback_with_tools = fallback_llm.bind_tools(
        [
            check_loan_amount,
            auto_approve_loan,
            request_human_approval,
        ]
    )
    return llm_with_tools.with_fallbacks([fallback_with_tools])


async def loan_checker(state: AgentState) -> dict[str, Any]:
    """Run the loan amount check and update the graph state."""
    check_result = await check_loan_amount.ainvoke(
        {
            "application_id": state["application_id"],
            "amount": state["amount"],
        }
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


def router(state: AgentState) -> Literal["auto_approver", "hitl_requester"]:
    """Route the application by amount after the policy check."""
    if state["amount"] <= HUMAN_APPROVAL_THRESHOLD:
        return "auto_approver"
    return "hitl_requester"


async def auto_approver(state: AgentState) -> dict[str, Any]:
    """Run the automatic approval tool for a low-value loan."""
    confirmation = await auto_approve_loan.ainvoke(
        {"application_id": state["application_id"]}
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
    confirmation = await request_human_approval.ainvoke(
        {
            "application_id": state["application_id"],
            "amount": state["amount"],
        }
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


def build_loan_graph():
    """Build and compile the loan approval StateGraph."""
    graph = StateGraph(AgentState)
    graph.add_node("loan_checker", loan_checker)
    graph.add_node("auto_approver", auto_approver)
    graph.add_node("hitl_requester", hitl_requester)

    graph.add_edge(START, "loan_checker")
    graph.add_conditional_edges(
        "loan_checker",
        router,
        {
            "auto_approver": "auto_approver",
            "hitl_requester": "hitl_requester",
        },
    )
    graph.add_edge("auto_approver", END)
    graph.add_edge("hitl_requester", END)
    return graph.compile()


loan_graph = build_loan_graph()


def initial_state(application_id: str, amount: float) -> AgentState:
    """Create a complete initial state for a loan application."""
    return {
        "application_id": application_id,
        "amount": amount,
        "status": "received",
        "human_required": False,
        "audit_entries": [],
    }


async def run_loan_application(
    application_id: str,
    amount: float,
) -> AgentState:
    """Run one loan application through the compiled graph."""
    result = await loan_graph.ainvoke(initial_state(application_id, amount))
    return result
