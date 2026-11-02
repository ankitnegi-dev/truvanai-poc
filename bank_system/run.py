# bank_system/run.py - CLI entry point for the bank loan agent.
#
# This package plays the role of the CUSTOMER. It does NOT import from
# shared/, tools/, agents/, hitl/, or ingest/.
#
# Usage:
#   python -m bank_system.run --amount 2000
#   python -m bank_system.run --amount 2000 --id LOAN-X
#
# Required environment variables for telemetry export:
#   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000
#     The SDK appends /v1/traces, so spans go to http://localhost:8000/v1/traces.
#   OTEL_EXPORTER_OTLP_HEADERS=x-truvanai-api-key=<RAW_KEY>
#     The raw ingest key printed by scripts/create_ingest_key.py.
#
# Optional:
#   BANK_APPROVAL_THRESHOLD=10000
#     The bank's own threshold. Default is 1000.
#     Set to 10000 to reproduce the bad-config scenario.

from __future__ import annotations

import argparse
import secrets
import sys

from bank_system.agent import process_loan
from bank_system.telemetry import flush_and_shutdown, setup_telemetry


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bank loan agent - sends OTel spans to TruvanAI."
    )
    parser.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Loan amount (e.g. 2000).",
    )
    parser.add_argument(
        "--id",
        dest="application_id",
        default=None,
        help="Loan application ID. Generated when not supplied.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    application_id = args.application_id
    if not application_id:
        application_id = f"LOAN-{secrets.token_hex(4).upper()}"

    # Set up the OTel exporter. Reads env vars automatically.
    provider = setup_telemetry()

    try:
        result = process_loan(application_id, args.amount)
    finally:
        # Flush and shut down to confirm all spans exported successfully.
        # With SimpleSpanProcessor the spans are already exported inline,
        # so this is a clean shutdown rather than a blocking wait.
        flush_and_shutdown(provider)

    # Print a clean summary.
    print()
    print("Bank Loan Agent - Summary")
    print("-" * 40)
    print(f"  ID:             {result['application_id']}")
    print(f"  Amount:         {result['amount']:,.2f}")
    print(f"  Bank threshold: {result['bank_threshold']:,.2f}")
    print(f"  Decision:       {result['decision']}")
    print(f"  Status:         {result['status']}")
    print("-" * 40)
    print()


if __name__ == "__main__":
    main()
