"""Create an ingest key for a tenant and print it once.

Usage:
    python scripts/create_ingest_key.py <tenant_id>

The raw key is printed exactly once and never stored. Copy it immediately.
Only the SHA-256 hash is persisted in the ingest_keys table.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import dispose_engine  # noqa: E402
from shared.telemetry_store import create_ingest_key, get_tenant  # noqa: E402


async def main(tenant_id: str) -> None:
    """Create and print the raw ingest key for the given tenant."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    try:
        tenant = await get_tenant(tenant_id)
        if tenant is None:
            print(
                f"ERROR: Tenant '{tenant_id}' does not exist. "
                "Run scripts/init_db.py first, or create the tenant.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        raw_key = await create_ingest_key(tenant_id)

        # Print the raw key once. This is the only time it is visible.
        print(f"Tenant:  {tenant_id}")
        print(f"Key:     {raw_key}")
        print()
        print("Copy this key immediately. It will not be shown again.")
        print("Set it as the x-truvanai-api-key header on OTLP requests.")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage: python scripts/create_ingest_key.py <tenant_id>",
            file=sys.stderr,
        )
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
