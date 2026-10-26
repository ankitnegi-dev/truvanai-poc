"""Initialize the TruvanAI PostgreSQL audit and trusted loan tables.

Run from the project root:

    python scripts/init_db.py

The script uses DATABASE_URL from .env and creates the ``agent_audit_log`` and
``loan_applications`` tables when they do not already exist. It is safe to run
more than once.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import (  # noqa: E402
    dispose_engine,
    get_audit_log_table_name,
    get_loan_table_columns,
    get_loan_table_name,
    initialize_database,
)

REQUIRED_LOAN_COLUMNS = frozenset(
    {
        "application_id",
        "customer_id",
        "amount",
        "status",
        "request_token_hash",
        "request_expires_at",
        "risk_flags",
    }
)


async def main() -> None:
    """Create and verify both tables, then close the connection pool."""
    try:
        print("Connecting to PostgreSQL.")
        await initialize_database()

        audit_table_name = await get_audit_log_table_name()
        if audit_table_name != "agent_audit_log":
            raise RuntimeError(
                "The agent_audit_log table was not found after initialization."
            )

        loan_table_name = await get_loan_table_name()
        if loan_table_name != "loan_applications":
            raise RuntimeError(
                "The loan_applications table was not found after initialization."
            )

        loan_columns = await get_loan_table_columns()
        missing_columns = REQUIRED_LOAN_COLUMNS - loan_columns
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise RuntimeError(
                "The loan_applications table is missing required columns: "
                f"{missing}. Recreate the local table with the current model."
            )

        print("Database initialized.")
        print("Table ready: agent_audit_log")
        print("Table ready: loan_applications")
    except Exception as exc:
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
