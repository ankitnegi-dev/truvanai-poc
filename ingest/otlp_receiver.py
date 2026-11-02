"""OTLP span receiver for TruvanAI.

Mounts as an APIRouter at /v1/traces inside the HITL dashboard. Accepts
ExportTraceServiceRequest protobuf payloads from a bank's OTel SDK, resolves
the tenant from the ingest key, stores spans, then calls the telemetry monitor.

Auth model
----------
- Ingest keys authenticate /v1/traces via the x-truvanai-api-key header
  (or Authorization: Bearer <key>).
- Ingest keys do NOT work on any other dashboard route.
- Dashboard Basic-auth / session cookies do NOT work on /v1/traces.
- These two auth domains are completely separate.

Limits
------
- 5 MB max request body.
- 1000 spans max per request.
- String attribute values truncated to 500 characters.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from shared.telemetry_store import (
    resolve_tenant_from_key,
    store_spans,
)

logger = logging.getLogger("truvanai.ingest")

# ---- limits ---------------------------------------------------------------
MAX_BODY_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_SPANS_PER_REQUEST = 1000
MAX_ATTR_STRING_LEN = 500

# ---- proto imports (lazy at module level for clear error messages) ---------
try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
        ExportTraceServiceResponse,
    )
    _PROTO_AVAILABLE = True
except ImportError:
    _PROTO_AVAILABLE = False
    ExportTraceServiceRequest = None  # type: ignore[assignment, misc]
    ExportTraceServiceResponse = None  # type: ignore[assignment, misc]

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth dependency - ingest key only
# ---------------------------------------------------------------------------


def _extract_raw_key(
    x_truvanai_api_key: str | None = Header(default=None, alias="x-truvanai-api-key"),
    authorization: str | None = Header(default=None, alias="authorization"),
) -> str | None:
    """Extract the raw ingest key from the request headers.

    Accepts:
      x-truvanai-api-key: <raw-key>
      Authorization: Bearer <raw-key>

    Returns the raw key string or None when neither header is present.
    """
    if x_truvanai_api_key:
        return x_truvanai_api_key.strip()
    if authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None


async def require_ingest_auth(
    raw_key: str | None = Depends(_extract_raw_key),
) -> str:
    """Resolve the tenant from the ingest key or raise 401.

    Returns the tenant_id on success. Never falls through to the dashboard
    auth layer; uses a completely separate credential lookup.
    """
    if not raw_key:
        raise HTTPException(
            status_code=401,
            detail="Missing ingest key. Provide x-truvanai-api-key header.",
        )

    tenant_id = await resolve_tenant_from_key(raw_key)
    if tenant_id is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked ingest key.",
        )
    return tenant_id


# ---------------------------------------------------------------------------
# Protobuf parsing
# ---------------------------------------------------------------------------


def _ids_to_hex(raw: bytes) -> str:
    """Convert raw span/trace id bytes to a lowercase hex string."""
    return raw.hex()


def _attr_value(av: Any) -> str | int | float | bool | None:
    """Extract a scalar from an AnyValue proto message.

    Returns None for unsupported types (array, kvlist, bytes).
    String values are truncated to MAX_ATTR_STRING_LEN characters.
    """
    kind = av.WhichOneof("value")
    if kind == "string_value":
        return av.string_value[:MAX_ATTR_STRING_LEN]
    if kind == "int_value":
        return int(av.int_value)
    if kind == "double_value":
        return float(av.double_value)
    if kind == "bool_value":
        return bool(av.bool_value)
    return None


def _kv_list_to_dict(kv_list: Any) -> dict[str, Any]:
    """Convert a repeated KeyValue sequence into a plain Python dict."""
    result: dict[str, Any] = {}
    for kv in kv_list:
        value = _attr_value(kv.value)
        if value is not None:
            result[kv.key] = value
    return result


def _ns_to_datetime(ns: int) -> datetime | None:
    """Convert a nanosecond Unix timestamp to a UTC-aware datetime.

    Returns None when the value is zero (unset).
    """
    if ns == 0:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def _parse_proto_body(
    body: bytes,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Parse an ExportTraceServiceRequest and return a flat list of span dicts.

    Raises HTTPException(400) for parse errors.
    The tenant_id comes exclusively from the ingest key; any tenant attribute
    in the payload is ignored.
    """
    if not _PROTO_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="opentelemetry-proto is not installed.",
        )

    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Malformed protobuf body: {exc}",
        ) from exc

    spans: list[dict[str, Any]] = []
    for resource_spans in req.resource_spans:
        # Read service.name from the resource attributes.
        resource_attrs = _kv_list_to_dict(resource_spans.resource.attributes)
        service_name = str(resource_attrs.get("service.name", "unknown"))

        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                trace_id = _ids_to_hex(span.trace_id)
                span_id = _ids_to_hex(span.span_id)
                parent_span_id = (
                    _ids_to_hex(span.parent_span_id)
                    if span.parent_span_id
                    else None
                )

                attrs = _kv_list_to_dict(span.attributes)

                spans.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "name": span.name[:255],
                        "service_name": service_name,
                        "start_time": _ns_to_datetime(span.start_time_unix_nano),
                        "end_time": _ns_to_datetime(span.end_time_unix_nano),
                        "attributes": attrs,
                    }
                )

    return spans


def _parse_json_body(
    body_text: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Parse a minimal JSON OTLP trace payload.

    Accepts the top-level shape:
      {"resourceSpans": [{"resource": {...}, "scopeSpans": [...]}]}

    This is a best-effort path for simple testing. Malformed JSON raises 400.
    """
    import json

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object.")

    spans: list[dict[str, Any]] = []
    for rs in payload.get("resourceSpans", []):
        res_attrs: dict[str, Any] = {}
        for kv in rs.get("resource", {}).get("attributes", []):
            v = kv.get("value", {})
            if "stringValue" in v:
                res_attrs[kv["key"]] = str(v["stringValue"])[:MAX_ATTR_STRING_LEN]
        service_name = str(res_attrs.get("service.name", "unknown"))

        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                attrs: dict[str, Any] = {}
                for kv in sp.get("attributes", []):
                    v = kv.get("value", {})
                    if "stringValue" in v:
                        attrs[kv["key"]] = str(v["stringValue"])[:MAX_ATTR_STRING_LEN]
                    elif "intValue" in v:
                        attrs[kv["key"]] = int(v["intValue"])
                    elif "doubleValue" in v:
                        attrs[kv["key"]] = float(v["doubleValue"])
                    elif "boolValue" in v:
                        attrs[kv["key"]] = bool(v["boolValue"])

                trace_id = sp.get("traceId", "")
                span_id = sp.get("spanId", "")
                parent_id = sp.get("parentSpanId") or None

                def _ns(v: Any) -> datetime | None:
                    try:
                        ns = int(v)
                        return _ns_to_datetime(ns) if ns else None
                    except (TypeError, ValueError):
                        return None

                spans.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent_id,
                        "name": str(sp.get("name", ""))[:255],
                        "service_name": service_name,
                        "start_time": _ns(sp.get("startTimeUnixNano")),
                        "end_time": _ns(sp.get("endTimeUnixNano")),
                        "attributes": attrs,
                    }
                )
    return spans


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/v1/traces")
async def receive_traces(
    request: Request,
    tenant_id: str = Depends(require_ingest_auth),
) -> Response:
    """Accept an OTLP ExportTraceServiceRequest from a bank's agent.

    Authentication: x-truvanai-api-key (or Authorization: Bearer <key>).
    Content-Type: application/x-protobuf (preferred) or application/json.
    Content-Encoding: gzip is supported.
    Size limit: 5 MB. Span limit: 1000 per request.

    Response: 200 with a serialized ExportTraceServiceResponse protobuf.
    The response is always 200 even when evaluation fails; evaluation
    errors are logged and spans stay with evaluated=false for the sweep.
    """
    # --- size guard --------------------------------------------------------
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds 5 MB limit.")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds 5 MB limit.")

    # --- decompress --------------------------------------------------------
    content_encoding = request.headers.get("content-encoding", "").lower()
    if content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to decompress gzip body: {exc}",
            ) from exc
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Decompressed body exceeds 5 MB limit.",
            )

    # --- parse body --------------------------------------------------------
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type == "application/x-protobuf":
        spans = _parse_proto_body(body, tenant_id)
    elif content_type == "application/json":
        spans = _parse_json_body(body.decode("utf-8", errors="replace"), tenant_id)
    else:
        raise HTTPException(
            status_code=415,
            detail=(
                "Unsupported content type. "
                "Use application/x-protobuf or application/json."
            ),
        )

    # --- span count guard --------------------------------------------------
    if len(spans) > MAX_SPANS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Request contains {len(spans)} spans, "
                f"which exceeds the limit of {MAX_SPANS_PER_REQUEST}."
            ),
        )

    # --- store spans -------------------------------------------------------
    inserted = 0
    if spans:
        inserted = await store_spans(tenant_id, spans)
        logger.info(
            "Ingested %d spans (%d new) for tenant %s",
            len(spans),
            inserted,
            tenant_id,
        )

    # --- call telemetry monitor (hook - Group D will replace this) ----------
    trace_ids = list({s["trace_id"] for s in spans})
    if trace_ids and inserted > 0:
        try:
            # GROUP D HOOK: replace this block with the real monitor call.
            # from agents.telemetry_monitor import evaluate_traces
            # await evaluate_traces(tenant_id, trace_ids)
            pass
        except Exception as exc:
            # Evaluation errors must not fail ingestion.
            logger.error(
                "Telemetry monitor evaluation failed for tenant %s traces %s: %s",
                tenant_id,
                trace_ids,
                exc,
            )

    # --- respond with protobuf ---------------------------------------------
    if _PROTO_AVAILABLE:
        resp_proto = ExportTraceServiceResponse()
        return Response(
            content=resp_proto.SerializeToString(),
            status_code=200,
            media_type="application/x-protobuf",
        )

    # Fallback for environments where proto is not installed.
    return Response(content=b"", status_code=200, media_type="application/x-protobuf")
