"""End-to-end scenario runner for the TruvanAI governance POC.

The orchestrator initializes the audit trail and runs the loan, data, and
monitor agents against real services. Each scenario is isolated so one failure
does not stop the remaining demonstration paths.
"""

from __future__ import annotations

import asyncio
import math
import os
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

RESULT_SEPARATOR = "-" * 60
SCENARIO_SEPARATOR = "=" * 60
LOAN_LABEL_WIDTH = 11
DATA_LABEL_WIDTH = 17


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


def _print_scenario_header(number: int, title: str) -> None:
    """Print a clear separator before each scenario."""
    print("\n" + SCENARIO_SEPARATOR)
    print(f"  SCENARIO {number} - {title}")
    print(SCENARIO_SEPARATOR)


async def main() -> None:
    """Run database setup and all three governance scenarios in order."""
    try:
        await init_db()
        print("Database initialized")
    except Exception as exc:
        print(f"Database initialization failed: {exc}")

    try:
        _print_scenario_header(1, "Auto-approval loan under 1000")
        result = await run_loan_agent(
            application_id="APP001",
            amount=500,
        )
        pretty_print_result(result)
        await run_monitor()
        print("Monitor check complete")
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 1 failed: {exc}")

    try:
        _print_scenario_header(2, "Large loan requiring HITL approval")
        result = await run_loan_agent(
            application_id="APP002",
            amount=5000,
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


if __name__ == "__main__":
    asyncio.run(main())
