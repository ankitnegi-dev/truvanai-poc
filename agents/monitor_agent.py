"""LangGraph monitor agent for TruvanAI governance enforcement.

The monitor checks recent audit entries, or a specific set of action IDs
reported through the external webhook, against two hardcoded policy rules.
The rules are action-based, so they apply equally to the POC's internal agents
and to an external bank agent that reports through ``POST /api/events``.
Matching actions are marked as blocked in PostgreSQL and sent to the Gmail
alert tool. Policy decisions remain deterministic; the configured Groq model
is available for explanation work but has no authority over blocking.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, update

from shared.db import (
    AgentAuditLog,
    get_actions_by_ids,
    get_db_connection,
    get_recent_actions,
    log_action,
)
from shared.policy import AUTO_APPROVE_ACTION, HUMAN_APPROVAL_THRESHOLD
from tools.notify import send_gmail_alert

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

AGENT_NAME = "monitor_agent"
LARGE_LOAN_RULE = "large_loan_no_human_approval"
SENSITIVE_DATA_RULE = "sensitive_data_access"
SENSITIVE_DATA_ACTION = "access_sensitive_data"

model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
fallback_model = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")


class AgentState(TypedDict):
    """State shared by the monitoring graph nodes."""

    recent_actions: list[dict[str, Any]]
    actions_to_check: list[dict[str, Any]]
    action_ids: list[int]
    violations_found: list[dict[str, Any]]
    actions_blocked: list[int]
    alerts_sent: list[str]
    monitoring_complete: bool
    agent_filter: str | None


def _build_groq_model(model_name: str) -> ChatGroq:
    """Create a ChatGroq model using the configured API key."""
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


def build_monitor_llm() -> Any:
    """Build the configured Groq model with its fallback."""
    primary = _build_groq_model(model)
    fallback = _build_groq_model(fallback_model)
    return primary.with_fallbacks([fallback])


@tool
async def block_agent_action(agent_id: str, action_id: int) -> str:
    """Mark an audited agent action as blocked and log the monitor action."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent_id must be a non-empty string.")
    if isinstance(action_id, bool) or not isinstance(action_id, int):
        raise TypeError("action_id must be an integer.")
    if action_id < 1:
        raise ValueError("action_id must be a positive integer.")

    clean_agent_id = agent_id.strip()
    statement = (
        update(AgentAuditLog)
        .where(AgentAuditLog.id == action_id)
        .values(policy_status="BLOCKED", verdict_card="NO")
        .returning(AgentAuditLog.id)
    )
    async with get_db_connection() as session:
        updated_id = await session.scalar(statement)
        if updated_id is None:
            await session.rollback()
            raise ValueError(f"Audit action {action_id} was not found.")
        await session.commit()

    confirmation = (
        f"Action {action_id} by {clean_agent_id} has been blocked"
    )
    await log_action(
        agent_name=AGENT_NAME,
        action_name="block_agent_action",
        parameters={
            "blocked_agent": clean_agent_id,
            "blocked_action_id": action_id,
        },
        result=confirmation,
        policy_status="ALLOWED",
        verdict_card="YES",
    )
    return confirmation


def _parameters_dict(action: Mapping[str, Any]) -> dict[str, Any]:
    """Return an audit action's parameters as a dictionary."""
    raw_parameters = action.get("parameters", {})
    if isinstance(raw_parameters, Mapping):
        return dict(raw_parameters)
    if isinstance(raw_parameters, str):
        try:
            parsed = json.loads(raw_parameters)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _amount_value(parameters: Mapping[str, Any]) -> float | None:
    """Extract a finite numeric amount from audit parameters."""
    value = parameters.get("amount")
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _application_id(parameters: Mapping[str, Any]) -> str | None:
    value = parameters.get("application_id")
    if value is None:
        return None
    clean_value = str(value).strip()
    return clean_value or None


def _make_violation(
    action: Mapping[str, Any],
    rule: str,
    parameters: dict[str, Any],
) -> dict[str, Any] | None:
    action_id = action.get("id")
    if isinstance(action_id, bool) or not isinstance(action_id, int):
        return None

    return {
        "rule": rule,
        "action_id": action_id,
        "agent_name": str(action.get("agent_name") or ""),
        "action_name": str(action.get("action_name") or ""),
        "parameters": parameters,
        "timestamp": str(action.get("timestamp") or ""),
        "verdict": "BLOCKED",
    }


def _large_loan_violation(
    action: Mapping[str, Any],
    recent_actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Apply the large loan auto approval rule to one audit action.

    The rule is action-based rather than agent-based. Any agent, including an
    external bank agent reporting through the webhook, that auto-approves an
    amount above the human approval threshold is a violation.
    """
    if action.get("action_name") != AUTO_APPROVE_ACTION:
        return None

    original_parameters = _parameters_dict(action)
    application_id = _application_id(original_parameters)
    if application_id is None:
        return None

    highest_amount: float | None = None
    amount_action_id: int | None = None
    for candidate in recent_actions:
        candidate_parameters = _parameters_dict(candidate)
        if _application_id(candidate_parameters) != application_id:
            continue
        amount = _amount_value(candidate_parameters)
        if amount is None or amount <= HUMAN_APPROVAL_THRESHOLD:
            continue
        if highest_amount is None or amount > highest_amount:
            highest_amount = amount
            candidate_id = candidate.get("id")
            amount_action_id = (
                candidate_id if isinstance(candidate_id, int) else None
            )

    if highest_amount is None:
        return None

    violation_parameters = dict(original_parameters)
    violation_parameters.update(
        {
            "amount": highest_amount,
            "human_approval_threshold": HUMAN_APPROVAL_THRESHOLD,
            "amount_source_action_id": amount_action_id,
        }
    )
    return _make_violation(action, LARGE_LOAN_RULE, violation_parameters)


def _sensitive_data_violation(
    action: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Apply the sensitive data access rule to one audit action.

    This rule is also action-based so an external agent cannot avoid the
    policy by using a different agent name in its webhook payload.
    """
    if action.get("action_name") != SENSITIVE_DATA_ACTION:
        return None
    return _make_violation(
        action,
        SENSITIVE_DATA_RULE,
        _parameters_dict(action),
    )


async def action_fetcher(state: AgentState) -> dict[str, Any]:
    """Fetch audit actions and select exactly which actions to check.

    Without an action filter the monitor checks the latest ten rows. With an
    action filter, as used by the webhook, it reads those rows directly and
    keeps a wider recent window for the amount-correlation rule.
    """
    action_ids = list(state.get("action_ids") or [])
    agent_filter = state.get("agent_filter")

    if action_ids:
        actions_to_check = await get_actions_by_ids(action_ids)
        recent_actions = await get_recent_actions(limit=50)
        merged_actions = {
            action["id"]: action for action in recent_actions
        }
        for action in actions_to_check:
            merged_actions[action["id"]] = action
        recent_actions = sorted(
            merged_actions.values(),
            key=lambda action: (action["timestamp"], action["id"]),
            reverse=True,
        )
    else:
        recent_actions = await get_recent_actions(limit=10)
        actions_to_check = recent_actions

    if agent_filter:
        actions_to_check = [
            action
            for action in actions_to_check
            if action.get("agent_name") == agent_filter
        ]
        recent_actions = [
            action
            for action in recent_actions
            if action.get("agent_name") == agent_filter
        ]

    return {
        "recent_actions": recent_actions,
        "actions_to_check": actions_to_check,
    }


async def _previously_blocked_action_ids(
    action_ids: set[int],
) -> set[int]:
    """Return action ids that already have a monitor block audit entry."""
    if not action_ids:
        return set()

    blocked_action_id = AgentAuditLog.parameters[
        "blocked_action_id"
    ].as_integer()
    statement = select(blocked_action_id).where(
        AgentAuditLog.agent_name == AGENT_NAME,
        AgentAuditLog.action_name == "block_agent_action",
        blocked_action_id.in_(sorted(action_ids)),
    )
    async with get_db_connection() as session:
        rows = (await session.scalars(statement)).all()
    return {
        int(value)
        for value in rows
        if value is not None
    }


async def policy_checker(state: AgentState) -> dict[str, Any]:
    """Check every selected action against both hardcoded policy rules."""
    violations: list[dict[str, Any]] = []
    recent_actions = state["recent_actions"]
    actions_to_check = state.get("actions_to_check") or recent_actions

    for action in actions_to_check:
        large_loan_violation = _large_loan_violation(action, recent_actions)
        if large_loan_violation is not None:
            violations.append(large_loan_violation)

        sensitive_data_violation = _sensitive_data_violation(action)
        if sensitive_data_violation is not None:
            violations.append(sensitive_data_violation)

    if violations:
        handled_action_ids = await _previously_blocked_action_ids(
            {violation["action_id"] for violation in violations}
        )
        violations = [
            violation
            for violation in violations
            if violation["action_id"] not in handled_action_ids
        ]

    return {"violations_found": violations}


async def violation_handler(state: AgentState) -> dict[str, Any]:
    """Block each violation and send a real Gmail alert."""
    blocked_action_ids: list[int] = []
    alerts: list[str] = []

    for violation in state["violations_found"]:
        action_id = violation["action_id"]
        agent_id = violation["agent_name"]

        block_confirmation = await block_agent_action.ainvoke(
            {
                "agent_id": agent_id,
                "action_id": action_id,
            }
        )
        if not isinstance(block_confirmation, str) or not block_confirmation:
            raise TypeError("block_agent_action returned an unexpected result.")

        alert_confirmation = await send_gmail_alert.ainvoke(
            {"violation_details": violation}
        )
        if not isinstance(alert_confirmation, str) or not alert_confirmation:
            raise TypeError("send_gmail_alert returned an unexpected result.")

        blocked_action_ids.append(action_id)
        alerts.append(alert_confirmation)

    return {
        "actions_blocked": blocked_action_ids,
        "alerts_sent": alerts,
    }


async def completion_node(state: AgentState) -> dict[str, Any]:
    """Mark the monitoring run as complete."""
    return {"monitoring_complete": True}


def route_after_policy_check(
    state: AgentState,
) -> Literal["violation_handler", "completion_node"]:
    """Route to the handler only when violations were found."""
    if state["violations_found"]:
        return "violation_handler"
    return "completion_node"


def build_monitor_graph():
    """Build and compile the governance monitor StateGraph."""
    graph = StateGraph(AgentState)
    graph.add_node("action_fetcher", action_fetcher)
    graph.add_node("policy_checker", policy_checker)
    graph.add_node("violation_handler", violation_handler)
    graph.add_node("completion_node", completion_node)

    graph.add_edge(START, "action_fetcher")
    graph.add_edge("action_fetcher", "policy_checker")
    graph.add_conditional_edges(
        "policy_checker",
        route_after_policy_check,
        {
            "violation_handler": "violation_handler",
            "completion_node": "completion_node",
        },
    )
    graph.add_edge("violation_handler", "completion_node")
    graph.add_edge("completion_node", END)
    return graph.compile()


monitor_graph = build_monitor_graph()


async def run_monitor(
    agent_name: str | None = None,
    action_ids: list[int] | None = None,
) -> AgentState:
    """Run the full monitor graph with optional agent and action filters.

    ``action_ids`` is used by the external webhook so a bank event is checked
    on its own rather than relying on whatever happens to be in the last ten
    audit rows. ``agent_name`` remains available for the internal demo.
    """
    clean_agent_name: str | None = None
    if agent_name is not None:
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("agent_name must be a non-empty string when set.")
        clean_agent_name = agent_name.strip()

    clean_action_ids: list[int] = []
    if action_ids is not None:
        if isinstance(action_ids, (str, bytes)) or not isinstance(
            action_ids,
            Iterable,
        ):
            raise TypeError("action_ids must be an iterable of integers.")
        for action_id in action_ids:
            if isinstance(action_id, bool) or not isinstance(action_id, int):
                raise TypeError("action_ids must contain only integers.")
            if action_id < 1:
                raise ValueError(
                    "action_ids must contain positive integers."
                )
            if action_id not in clean_action_ids:
                clean_action_ids.append(action_id)

    result = await monitor_graph.ainvoke(
        {
            "recent_actions": [],
            "actions_to_check": [],
            "action_ids": clean_action_ids,
            "violations_found": [],
            "actions_blocked": [],
            "alerts_sent": [],
            "monitoring_complete": False,
            "agent_filter": clean_agent_name,
        }
    )
    return result

