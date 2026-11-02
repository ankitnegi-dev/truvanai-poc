# bank_system/telemetry.py - OpenTelemetry setup for the bank agent.
#
# This package plays the role of the CUSTOMER. It does NOT import from
# shared/, tools/, agents/, hitl/, or ingest/.
#
# Endpoint behavior (confirmed by testing with SDK 1.45.1):
#   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000
#   The SDK appends /v1/traces, so it exports to http://localhost:8000/v1/traces.
#   Do NOT include /v1/traces in OTEL_EXPORTER_OTLP_ENDPOINT.
#
# Auth header:
#   OTEL_EXPORTER_OTLP_HEADERS=x-truvanai-api-key=<RAW_KEY>
#   The exporter passes this header on every POST.

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import SERVICE_NAME as RESOURCE_SERVICE_NAME
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

SERVICE_NAME = "bank-loan-agent"

_provider: TracerProvider | None = None


def setup_telemetry() -> TracerProvider:
    """Create and register the global TracerProvider with an OTLP HTTP exporter.

    Reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS from the
    environment (standard OTel env var names). Returns the provider so the
    caller can call force_flush() and shutdown() before the process exits.

    When OTEL_EXPORTER_OTLP_ENDPOINT is not set, the exporter defaults to
    localhost:4318 (the standard OTel Collector HTTP port). The bank_system
    demo expects it to point at the TruvanAI dashboard.
    """
    global _provider

    resource = Resource.create({RESOURCE_SERVICE_NAME: SERVICE_NAME})

    exporter = OTLPSpanExporter()
    # SimpleSpanProcessor exports each span synchronously when it ends.
    # This ensures no span is lost when the process exits, and removes the
    # need to tune BatchSpanProcessor flush timing in a short-lived CLI.
    processor = SimpleSpanProcessor(exporter)

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    """Return a tracer for the bank loan agent service."""
    return trace.get_tracer(SERVICE_NAME)


def flush_and_shutdown(provider: TracerProvider) -> None:
    """Force all pending spans to export, then shut down the provider.

    Must be called before the process exits so no span is lost.
    """
    provider.force_flush(timeout_millis=10_000)
    provider.shutdown()
