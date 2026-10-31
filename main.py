"""End-to-end scenario runner for the TruvanAI governance POC.

The orchestrator initializes the audit trail and runs the loan, data, and
monitor agents against real services. Each scenario is isolated so one failure
does not stop the remaining demonstration paths.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import urllib.error
import urllib.request
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import langchain_core  # noqa: F401
from dotenv import load_dotenv

# LangChain registers its own warning filter during import. Apply these after
# langchain_core and before the agent imports so the requested filters win.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from agents.data_agent import run_data_agent  # noqa: E402
from agents.loan_agent import (  # noqa: E402
    run_loan_application as run_loan_agent,
)
from agents.monitor_agent import run_monitor  # noqa: E402
from scripts.init_db import initialize_database as init_db  # noqa: E402
from shared.loan_store import (  # noqa: E402
    LoanRequestDenied,
    delete_loan_application,
    issue_loan_request,
)
from shared.policy import (  # noqa: E402
    HITL_APPROVED_QUEUE,
    HITL_QUEUE,
    HITL_REJECTED_QUEUE,
)
from tools.loan_tools import check_loan_amount, get_redis_client  # noqa: E402

RESULT_SEPARATOR = "-" * 60
SCENARIO_SEPARATOR = "=" * 60
LOAN_LABEL_WIDTH = 11
DATA_LABEL_WIDTH = 17
WEBHOOK_LABEL_WIDTH = 15
DEFAULT_DASHBOARD_URL = "http://localhost:8000"


def _result_text(value: Any) -> str:
    """Convert a tool result into one clean display value."""
    if isinstance(value, Mapping):
        value = value.get("message", "Completed")
    text = str(value).strip()
    if not text:
        return "Completed"
    return text[:-1] if text.endswith(".") else text


def _print_field(label: str, value: str, width: int) -> None:
    """Print one aligned result field."""
    print(f"  {label:<{width}} : {value}")


def _audit_entries(
    result: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return the result's audit entries that can be displayed."""
    entries = result.get("audit_entries", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _loan_step_description(entry: Mapping[str, Any]) -> str:
    """Format one loan audit step for display."""
    tool = str(entry.get("tool", "unknown"))
    result = entry.get("result")
    if tool == "check_loan_amount" and isinstance(result, Mapping):
        if result.get("human_required") is True:
            return "Application requires human approval"
        return "Application within auto-approval limit"
    return _result_text(result)


def _print_steps(
    entries: list[Mapping[str, Any]],
    label_width: int,
    loan_result: bool,
) -> None:
    """Print numbered audit steps with readable descriptions."""
    print(f"  {'Steps taken':<{label_width}} :")
    if not entries:
        print("    No audit steps recorded")
        return

    for index, entry in enumerate(entries, start=1):
        tool = str(entry.get("tool", "unknown"))
        description = (
            _loan_step_description(entry)
            if loan_result
            else _result_text(entry.get("result"))
        )
        lines = description.splitlines() or ["Completed"]
        print(f"    {index}. {tool}  -> {lines[0]}")
        for line in lines[1:]:
            print(f"       {line}")


def _format_amount(value: Any) -> str:
    """Format a loan amount as US dollars."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "Unknown"
    if not math.isfinite(amount):
        return "Unknown"
    return f"${amount:,.2f}"


def _print_loan_result(result: Mapping[str, Any]) -> None:
    """Print a loan agent result in a readable layout."""
    status = str(result.get("status", "unknown")).upper()
    human_required = "Yes" if result.get("human_required") is True else "No"

    print(RESULT_SEPARATOR)
    _print_field(
        "Application",
        str(result.get("application_id", "Unknown")),
        LOAN_LABEL_WIDTH,
    )
    _print_field(
        "Amount",
        _format_amount(result.get("amount")),
        LOAN_LABEL_WIDTH,
    )
    _print_field("Status", status, LOAN_LABEL_WIDTH)
    _print_field("HITL needed", human_required, LOAN_LABEL_WIDTH)
    if status == "BLOCKED":
        _print_field(
            "Gate reason",
            str(result.get("blocked_reason", "blocked")),
            LOAN_LABEL_WIDTH,
        )
    _print_steps(
        _audit_entries(result),
        LOAN_LABEL_WIDTH,
        loan_result=True,
    )
    print(RESULT_SEPARATOR)


def _record_count(result: Mapping[str, Any]) -> int:
    """Count the records returned by the approved overdue accounts file."""
    raw_data = result.get("raw_data", "")
    if not isinstance(raw_data, str):
        return 0
    return len([line for line in raw_data.splitlines() if line.strip()])


def _print_data_result(result: Mapping[str, Any]) -> None:
    """Print a restricted data agent result in a readable layout."""
    violation_attempt = (
        "Yes" if result.get("violation_attempted") is True else "No"
    )

    print(RESULT_SEPARATOR)
    _print_field(
        "Task",
        str(result.get("task", "Unknown")),
        DATA_LABEL_WIDTH,
    )
    _print_field(
        "Violation attempt",
        violation_attempt,
        DATA_LABEL_WIDTH,
    )
    _print_field(
        "Records read",
        str(_record_count(result)),
        DATA_LABEL_WIDTH,
    )
    _print_steps(
        _audit_entries(result),
        DATA_LABEL_WIDTH,
        loan_result=False,
    )
    print(RESULT_SEPARATOR)


def pretty_print_result(result: Mapping[str, Any]) -> None:
    """Print a loan or data agent result without raw Python dictionaries."""
    if "application_id" in result:
        _print_loan_result(result)
        return
    if "task" in result:
        _print_data_result(result)
        return
    print(RESULT_SEPARATOR)
    print("  Result format not recognized")
    print(RESULT_SEPARATOR)


def _dashboard_url() -> str:
    """Return the dashboard base URL used by the webhook scenario."""
    value = os.getenv("DASHBOARD_URL", DEFAULT_DASHBOARD_URL).strip()
    return (value or DEFAULT_DASHBOARD_URL).rstrip("/")


def _post_webhook_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Post one external agent event to the governance webhook."""
    api_key = os.getenv("WEBHOOK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "WEBHOOK_API_KEY is not set. Add it to .env and restart the "
            "dashboard before running Scenario 4."
        )

    request = urllib.request.Request(
        f"{_dashboard_url()}/api/events",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Webhook returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Dashboard is not reachable at {_dashboard_url()}."
        ) from exc

    result = json.loads(body)
    if not isinstance(result, dict):
        raise RuntimeError("Webhook returned an unexpected response.")
    return result


async def _post_webhook_event_async(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the blocking webhook call outside the event loop."""
    return await asyncio.to_thread(_post_webhook_event, payload)


def _print_webhook_result(result: Mapping[str, Any]) -> None:
    """Print one external webhook verdict in a readable layout."""
    rules = result.get("rules_triggered")
    if isinstance(rules, list) and rules:
        rules_text = ", ".join(str(rule) for rule in rules)
    else:
        rules_text = "None"

    print(RESULT_SEPARATOR)
    _print_field(
        "Event",
        str(result.get("event_id", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Agent",
        str(result.get("agent", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Action",
        str(result.get("action", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Application",
        str(result.get("application_id", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Amount",
        _format_amount(result.get("amount")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Verdict",
        str(result.get("verdict", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field(
        "Verdict card",
        str(result.get("verdict_card", "Unknown")),
        WEBHOOK_LABEL_WIDTH,
    )
    _print_field("Rules triggered", rules_text, WEBHOOK_LABEL_WIDTH)
    _print_field(
        "Alerts sent",
        str(result.get("alerts_sent", 0)),
        WEBHOOK_LABEL_WIDTH,
    )
    print(RESULT_SEPARATOR)


def _print_scenario_header(number: int, title: str) -> None:
    """Print a clear separator before each scenario."""
    print("\n" + SCENARIO_SEPARATOR)
    print(f"  SCENARIO {number} - {title}")
    print(SCENARIO_SEPARATOR)


async def _remove_demo_queue_items(application_id: str) -> None:
    """Remove stale demo entries before issuing a fresh trusted request."""
    client = get_redis_client()
    for queue_name in (
        HITL_QUEUE,
        HITL_APPROVED_QUEUE,
        HITL_REJECTED_QUEUE,
    ):
        raw_items = await client.lrange(queue_name, 0, -1)
        for raw_item in raw_items:
            try:
                item = json.loads(raw_item)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(item, dict)
                and item.get("application_id") == application_id
            ):
                await client.lrem(queue_name, 1, raw_item)


async def _seed_demo_loan_request(
    application_id: str,
    amount: float,
) -> str:
    """Model trusted intake and return only the opaque request token."""
    try:
        await _remove_demo_queue_items(application_id)
    except Exception:
        print(
            f"Warning: stale queue entries for {application_id} "
            "could not be cleared."
        )

    await delete_loan_application(application_id)
    issued = await issue_loan_request(
        application_id=application_id,
        customer_id=f"CUST-{application_id}",
        amount=amount,
    )
    return issued.request_token


async def _demonstrate_gate_tamper(
    application_id: str,
    request_token: str,
    claimed_amount: float,
) -> None:
    """Show that a tampered amount is blocked before it reaches HITL."""
    try:
        await check_loan_amount.ainvoke(
            {
                "application_id": application_id,
                "request_token": request_token,
                "amount": claimed_amount,
            }
        )
    except LoanRequestDenied as exc:
        reason = exc.decision.reason or "blocked"
        print(
            "Preflight gate blocked tampered amount claim for "
            f"{application_id} ({reason})"
        )
        return
    raise RuntimeError(
        "Preflight gate failed to block a tampered amount claim."
    )


async def main() -> None:
    """Run database setup and all four governance scenarios in order."""
    try:
        await init_db()
        print("Database initialized")
    except Exception as exc:
        print(f"Database initialization failed: {exc}")

    try:
        _print_scenario_header(1, "Auto-approval loan under 1000")
        token = await _seed_demo_loan_request("APP001", 500.0)
        result = await run_loan_agent(
            application_id="APP001",
            request_token=token,
        )
        pretty_print_result(result)
        await run_monitor()
        print("Monitor check complete")
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 1 failed: {exc}")

    try:
        _print_scenario_header(2, "Large loan requiring HITL approval")
        token = await _seed_demo_loan_request("APP002", 5000.0)
        await _demonstrate_gate_tamper("APP002", token, 500.0)
        result = await run_loan_agent(
            application_id="APP002",
            request_token=token,
        )
        pretty_print_result(result)
        await run_monitor()
        print(
            "Monitor check complete - loan pending human approval in dashboard"
        )
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 2 failed: {exc}")

    try:
        _print_scenario_header(3, "Data agent policy violation")
        simulate_violation = (
            os.getenv("SIMULATE_VIOLATION", "false").strip().lower() == "true"
        )

        if simulate_violation:
            result = await run_data_agent(
                task="Read overdue accounts and summarize"
            )
            pretty_print_result(result)
            await run_monitor(agent_name="data_agent")
            print("Monitor check complete - violation detected and blocked")
        else:
            print(
                "SIMULATE_VIOLATION is false - skipping violation scenario"
            )
            result = await run_data_agent(
                task="Read overdue accounts and summarize"
            )
            pretty_print_result(result)
            await run_monitor()
            print("Monitor check complete")
    except Exception as exc:
        print(f"Scenario 3 failed: {exc}")

    try:
        _print_scenario_header(4, "External bank agent webhook")
        print(f"Dashboard endpoint: {_dashboard_url()}/api/events")

        allowed_event = {
            "agent": "bank_loan_agent",
            "action": "auto_approve_loan",
            "application_id": "EXT-APP-LOW",
            "amount": 750.0,
            "parameters": {
                "channel": "external_webhook",
                "customer_segment": "retail",
            },
        }
        allowed_result = await _post_webhook_event_async(allowed_event)
        _print_webhook_result(allowed_result)
        if allowed_result.get("verdict") != "ALLOWED":
            raise RuntimeError(
                "The monitor did not allow the low-value external event."
            )

        violation_event = {
            "agent": "bank_loan_agent",
            "action": "auto_approve_loan",
            "application_id": "EXT-APP-HIGH",
            "amount": 2500.0,
            "parameters": {
                "channel": "external_webhook",
                "customer_segment": "retail",
            },
        }
        violation_result = await _post_webhook_event_async(
            violation_event
        )
        _print_webhook_result(violation_result)
        if violation_result.get("verdict") != "BLOCKED":
            raise RuntimeError(
                "The monitor did not block the high-value external event."
            )
        print(
            "Monitor blocked the external action and sent the Gmail alert"
        )
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 4 failed: {exc}")
        print(
            "Start the dashboard first: "
            "uvicorn hitl.dashboard:app --port 8000"
        )


if __name__ == "__main__":
    asyncio.run(main())
