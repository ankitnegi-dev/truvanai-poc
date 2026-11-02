# bank_system/config.py - bank-side configuration.
#
# This package plays the role of the CUSTOMER. It does NOT import from
# shared/, tools/, agents/, hitl/, or ingest/.
#
# BANK_APPROVAL_THRESHOLD is the bank's own deployed setting.
# The correct value is 1000 (matching TruvanAI's policy).
# Setting it to 10000 is the realistic bad-config scenario: the bank's agent
# auto-approves a 2000 loan, which TruvanAI catches from telemetry alone.

from __future__ import annotations

import os

# Read from the environment so integration tests can override it.
# The default matches the bank's documented policy of 1000.
# Set BANK_APPROVAL_THRESHOLD=10000 to reproduce the bad-config scenario.
BANK_APPROVAL_THRESHOLD: float = float(
    os.getenv("BANK_APPROVAL_THRESHOLD", "1000")
)
