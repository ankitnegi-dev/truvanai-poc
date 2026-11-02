"""Initialize TruvanAI PostgreSQL tables.

Run from the project root:

    python scripts/init_db.py

Creates the following tables idempotently:
  - agent_audit_log
  - loan_applications
  - tenants
  - tenant_policies
  - ingest_keys
  - telemetry_spans
  - governance_violations

Also seeds the "bank-demo" tenant with a policy threshold of 1000 INR if it
does not already exist. The owner_email is read from GMAIL_RECIPIENT in .env.
"""

from __future__ import annotations

import asyncio
import os
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
from shared.telemetry_store import (  # noqa: E402
    create_tenant,
    get_policy,
    get_tenant,
    get_telemetry_table_names,
    initialize_telemetry_tables,
    set_policy,
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

EXPECTED_TELEMETRY_TABLES = frozenset(
    {
        "tenants",
        "tenant_policies",
        "ingest_keys",
        "telemetry_spans",
        "governance_violations",
    }
)

DEMO_TENANT_ID = "bank-demo"
DEMO_DISPLAY_NAME = "Bank Demo Tenant"
DEMO_THRESHOLD = 1000.0
DEMO_CURRENCY = "INR"


async def main() -> None:
    """Create and verify all tables, seed demo tenant, close pool."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    try:
        print("Connecting to PostgreSQL.")

        # Core TruvanAI tables (audit log, loan applications).
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

        # OTel tables.
        await initialize_telemetry_tables()

        telemetry_tables = await get_telemetry_table_names()
        missing_tables = EXPECTED_TELEMETRY_TABLES - telemetry_tables
        if missing_tables:
            missing = ", ".join(sorted(missing_tables))
            raise RuntimeError(
                f"OTel tables were not created: {missing}."
            )

        print("Database initialized.")
        print("Table ready: agent_audit_log")
        print("Table ready: loan_applications")
        for table_name in sorted(EXPECTED_TELEMETRY_TABLES):
            print(f"Table ready: {table_name}")

        # Seed bank-demo tenant.
        owner_email = os.getenv("GMAIL_RECIPIENT", "").strip()
        if not owner_email:
            print(
                "WARNING: GMAIL_RECIPIENT is not set. "
                "bank-demo owner_email will be 'demo@example.com'."
            )
            owner_email = "demo@example.com"

        existing_tenant = await get_tenant(DEMO_TENANT_ID)
        if existing_tenant is None:
            await create_tenant(
                tenant_id=DEMO_TENANT_ID,
                display_name=DEMO_DISPLAY_NAME,
                owner_email=owner_email,
            )
            print(f"Tenant created: {DEMO_TENANT_ID}")
        else:
            print(f"Tenant already exists: {DEMO_TENANT_ID}")

        existing_policy = await get_policy(DEMO_TENANT_ID)
        if existing_policy is None:
            await set_policy(
                tenant_id=DEMO_TENANT_ID,
                approval_threshold=DEMO_THRESHOLD,
                currency=DEMO_CURRENCY,
            )
            print(
                f"Policy set: {DEMO_TENANT_ID} "
                f"threshold={DEMO_THRESHOLD} currency={DEMO_CURRENCY}"
            )
        else:
            print(
                f"Policy already set: {DEMO_TENANT_ID} "
                f"threshold={existing_policy['approval_threshold']} "
                f"currency={existing_policy['currency']}"
            )

    except Exception as exc:
        print(f"Database initialization failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
