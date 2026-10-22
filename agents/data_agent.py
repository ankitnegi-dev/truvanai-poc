"""LangGraph agent for the restricted overdue accounts workflow.

The normal path reads the approved overdue accounts file and asks Groq for a
summary. The summarizer tool uses ChatGroq with GROQ_MODEL and the configured
fallback model. When SIMULATE_VIOLATION is enabled, the graph takes the policy
test path and attempts a sensitive field access, which the tool always blocks.
"""

from __future__ import annotations

import operator
import os
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from tools.data_tools import (
    access_sensitive_data,
    read_overdue_accounts,
    summarize_accounts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class AgentState(TypedDict):
    """State shared by the restricted data agent nodes."""

    task: str
    raw_data: str
    summary: str
    violation_attempted: bool
    audit_entries: Annotated[list[dict[str, Any]], operator.add]


async def data_reader(state: AgentState) -> dict[str, Any]:
    """Read the approved overdue accounts file."""
    raw_data = await read_overdue_accounts.ainvoke({})
    if not isinstance(raw_data, str):
        raise TypeError("read_overdue_accounts returned an unexpected result.")

    return {
        "raw_data": raw_data,
        "audit_entries": [
            {
                "node": "data_reader",
                "tool": "read_overdue_accounts",
                "task": state["task"],
                "result": f"Read {len(raw_data.splitlines())} account records.",
            }
        ],
    }


async def summarizer(state: AgentState) -> dict[str, Any]:
    """Summarize the approved overdue accounts with Groq."""
    summary = await summarize_accounts.ainvoke(
        {"raw_data": state["raw_data"]}
    )
    if not isinstance(summary, str) or not summary.strip():
        raise TypeError("summarize_accounts returned an unexpected result.")

    return {
        "summary": summary.strip(),
        "violation_attempted": False,
        "audit_entries": [
            {
                "node": "summarizer",
                "tool": "summarize_accounts",
                "result": summary.strip(),
            }
        ],
    }


async def violation_node(state: AgentState) -> dict[str, Any]:
    """Attempt a sensitive field access and record the policy denial."""
    denial = await access_sensitive_data.ainvoke(
        {"field": "aadhaar_number"}
    )
    if not isinstance(denial, str):
        raise TypeError("access_sensitive_data returned an unexpected result.")

    return {
        "violation_attempted": True,
        "audit_entries": [
            {
                "node": "violation_node",
                "tool": "access_sensitive_data",
                "field": "aadhaar_number",
                "result": denial,
            }
        ],
    }


def route_after_reader(
    state: AgentState,
) -> Literal["violation_node", "summarizer"]:
    """Choose the violation test path or the normal summary path."""
    if os.getenv("SIMULATE_VIOLATION", "false").lower() == "true":
        return "violation_node"
    return "summarizer"


def build_data_graph():
    """Build and compile the restricted data StateGraph."""
    graph = StateGraph(AgentState)
    graph.add_node("data_reader", data_reader)
    graph.add_node("summarizer", summarizer)
    graph.add_node("violation_node", violation_node)

    graph.add_edge(START, "data_reader")
    graph.add_conditional_edges(
        "data_reader",
        route_after_reader,
        {
            "violation_node": "violation_node",
            "summarizer": "summarizer",
        },
    )
    graph.add_edge("summarizer", END)
    graph.add_edge("violation_node", END)
    return graph.compile()


data_graph = build_data_graph()


def initial_state(task: str) -> AgentState:
    """Create a complete initial state for one data agent task."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string.")
    return {
        "task": task.strip(),
        "raw_data": "",
        "summary": "",
        "violation_attempted": False,
        "audit_entries": [],
    }


async def run_data_agent(task: str) -> AgentState:
    """Run one task through the compiled restricted data graph."""
    result = await data_graph.ainvoke(initial_state(task))
    return result
