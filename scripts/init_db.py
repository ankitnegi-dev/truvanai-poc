"""Initialize the TruvanAI PostgreSQL audit trail.

Run from the project root:

    python scripts/init_db.py

The script uses DATABASE_URL from .env and creates the agent_audit_log table
when it does not already exist. It is safe to run more than once.
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
    initialize_database,
)


async def main() -> None:
    """Create and verify the audit trail table, then close the pool."""
    try:
        print("Connecting to PostgreSQL.")
        await initialize_database()

        table_name = await get_audit_log_table_name()
        if table_name != "agent_audit_log":
            raise RuntimeError(
                "The agent_audit_log table was not found after initialization."
            )

        print("Database initialized.")
        print("Table ready: agent_audit_log")
    except Exception as exc:
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
