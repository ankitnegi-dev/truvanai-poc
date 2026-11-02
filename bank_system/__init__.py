# bank_system - simulates a bank customer that TruvanAI governs.
#
# This package plays the role of the CUSTOMER. It has its own SQLite store,
# its own approval threshold config, and uses only stdlib and opentelemetry
# packages. It does NOT import from shared/, tools/, agents/, hitl/, or
# ingest/. TruvanAI observes it through OpenTelemetry spans only.
