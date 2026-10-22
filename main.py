"""End-to-end scenario runner for the TruvanAI governance POC.

The orchestrator initializes the audit trail and runs the loan, data, and
monitor agents against real services. Each scenario is isolated so one failure
does not stop the remaining demonstration paths.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from agents.data_agent import run_data_agent  # noqa: E402
from agents.loan_agent import (  # noqa: E402
    run_loan_application as run_loan_agent,
)
from agents.monitor_agent import run_monitor  # noqa: E402
from scripts.init_db import initialize_database as init_db  # noqa: E402


async def main() -> None:
    """Run database setup and all three governance scenarios in order."""
    try:
        await init_db()
        print("Database initialized")
    except Exception as exc:
        print(f"Database initialization failed: {exc}")

    try:
        print("--- Scenario 1: Auto-approval loan under 1000 ---")
        result = await run_loan_agent(
            application_id="APP001",
            amount=500,
        )
        print(result)
        await run_monitor()
        print("Monitor check complete")
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 1 failed: {exc}")

    try:
        print("--- Scenario 2: Large loan requiring HITL approval ---")
        result = await run_loan_agent(
            application_id="APP002",
            amount=5000,
        )
        print(result)
        await run_monitor()
        print(
            "Monitor check complete - loan pending human approval in dashboard"
        )
        await asyncio.sleep(2)
    except Exception as exc:
        print(f"Scenario 2 failed: {exc}")

    try:
        print("--- Scenario 3: Data agent policy violation ---")
        simulate_violation = (
            os.getenv("SIMULATE_VIOLATION", "false").strip().lower() == "true"
        )

        if simulate_violation:
            result = await run_data_agent(
                task="Read overdue accounts and summarize"
            )
            print(result)
            await run_monitor(agent_name="data_agent")
            print("Monitor check complete - violation detected and blocked")
        else:
            print(
                "SIMULATE_VIOLATION is false - skipping violation scenario"
            )
            result = await run_data_agent(
                task="Read overdue accounts and summarize"
            )
            print(result)
            await run_monitor()
            print("Monitor check complete")
    except Exception as exc:
        print(f"Scenario 3 failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
