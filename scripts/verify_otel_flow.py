"""End-to-end verifier for the TruvanAI OpenTelemetry integration.

The verifier uses real PostgreSQL, the real FastAPI app, a real uvicorn
server, real OTLP/HTTP delivery from the bank agent, and the real telemetry
monitor. It creates a temporary tenant named verify-tenant with a policy of
1000 INR, runs the 17 checks from the specification, removes the temporary
tenant's spans, violations, and keys, and leaves the append-only audit rows
identifiable by tenant.

ALERTS_ENABLED is forced to false so the verifier never sends email.

Run with:

    python scripts/verify_otel_flow.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")
os.environ["ALERTS_ENABLED"] = "false"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ["BANK_CONTROL_SECRET"] = "verify-containment-secret"

import httpx
import uvicorn
from sqlalchemy import delete, func, select, update

from shared.db import AgentAuditLog, get_db_connection
from shared.telemetry_store import (
    GovernanceViolation,
    IngestKey,
    TelemetrySpan,
    Tenant,
    TenantPolicy,
    create_ingest_key,
    create_tenant,
    set_policy,
)

TENANT = "verify-tenant"
POLICY_THRESHOLD = 1000.0
MAX_BODY_BYTES = 5 * 1024 * 1024

RESULTS: list[tuple[int, str, str, str]] = []


def record(
    number: int,
    name: str,
    ok: bool,
    detail: str = "",
    skipped: bool = False,
) -> None:
    status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    RESULTS.append((number, name, status, detail))
    print(f"  {status}  {number}. {name}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Server thread with its own event loop
# ---------------------------------------------------------------------------


class ServerThread:
    """Run uvicorn in a background thread and expose its event loop."""

    def __init__(self, app: Any, port: int) -> None:
        self.app = app
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="verify-otel-server",
            daemon=True,
        )
        self.ready = threading.Event()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        self.server = uvicorn.Server(config)
        self.ready.set()
        try:
            loop.run_until_complete(self.server.serve())
        finally:
            loop.close()

    def start(self, timeout: float = 20.0) -> None:
        self.thread.start()
        if not self.ready.wait(timeout):
            raise RuntimeError("The verifier server thread did not start.")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server is not None and self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("The verifier uvicorn server did not start.")

    def run(self, coroutine: Any, timeout: float = 120.0) -> Any:
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError("The verifier server loop is not running.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        self.thread.join(timeout=20)


# ---------------------------------------------------------------------------
# Payloads and subprocess helpers
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def hex_id(length: int) -> str:
    return uuid.uuid4().hex[:length]


def otlp_json(spans: list[dict[str, Any]]) -> bytes:
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "bank-loan-agent"},
                        }
                    ]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def otlp_span(
    trace_id: str,
    span_id: str,
    name: str,
    application_id: str,
    amount: float | None,
    decision: str | None = None,
    extra_attributes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attributes: list[dict[str, Any]] = [
        {
            "key": "loan.application_id",
            "value": {"stringValue": application_id},
        }
    ]
    if amount is not None:
        attributes.append(
            {"key": "loan.amount", "value": {"doubleValue": amount}}
        )
    if decision is not None:
        attributes.append(
            {"key": "loan.decision", "value": {"stringValue": decision}}
        )
    if extra_attributes:
        attributes.extend(extra_attributes)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "attributes": attributes,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000001000000000",
    }


def run_bank(
    port: int,
    key: str,
    amount: str,
    application_id: str,
    threshold: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{port}"
    env["OTEL_EXPORTER_OTLP_HEADERS"] = f"x-truvanai-api-key={key}"
    env["BANK_APPROVAL_THRESHOLD"] = threshold
    env["ALERTS_ENABLED"] = "false"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bank_system.run",
            "--amount",
            amount,
            "--id",
            application_id,
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def post_traces(
    client: httpx.Client,
    key: str | None,
    payload: bytes,
    content_type: str = "application/json",
) -> httpx.Response:
    headers = {"content-type": content_type}
    if key is not None:
        headers["x-truvanai-api-key"] = key
    return client.post("/v1/traces", content=payload, headers=headers)


def oversized_status(port: int, key: str) -> int | None:
    """Send only headers with a too-large Content-Length and read the status."""
    request = (
        "POST /v1/traces HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"x-truvanai-api-key: {key}\r\n"
        "Content-Type: application/x-protobuf\r\n"
        f"Content-Length: {MAX_BODY_BYTES + 1}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
        sock.sendall(request)
        sock.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

    if not data:
        return None
    first_line = data.split(b"\r\n", 1)[0].decode("latin-1")
    parts = first_line.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Database helpers that run on the server loop
# ---------------------------------------------------------------------------


async def set_tenant_control_url(control_url: str) -> None:
    async with get_db_connection() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.tenant_id == TENANT)
            .values(control_url=control_url)
        )
        await session.commit()


async def cleanup_tenant_data() -> None:
    async with get_db_connection() as session:
        await session.execute(
            delete(GovernanceViolation).where(
                GovernanceViolation.tenant_id == TENANT
            )
        )
        await session.execute(
            delete(TelemetrySpan).where(TelemetrySpan.tenant_id == TENANT)
        )
        await session.execute(
            delete(IngestKey).where(IngestKey.tenant_id == TENANT)
        )
        await session.execute(
            delete(TenantPolicy).where(TenantPolicy.tenant_id == TENANT)
        )
        await session.execute(delete(Tenant).where(Tenant.tenant_id == TENANT))
        await session.commit()
    async with get_db_connection() as session:
        await session.execute(
            delete(GovernanceViolation).where(
                GovernanceViolation.tenant_id == TENANT
            )
        )
        await session.execute(
            delete(TelemetrySpan).where(TelemetrySpan.tenant_id == TENANT)
        )
        await session.execute(
            delete(IngestKey).where(IngestKey.tenant_id == TENANT)
        )
        await session.execute(
            delete(TenantPolicy).where(TenantPolicy.tenant_id == TENANT)
        )
        await session.execute(delete(Tenant).where(Tenant.tenant_id == TENANT))
        await session.commit()


async def create_verify_tenant() -> str:
    await create_tenant(
        TENANT,
        "OpenTelemetry flow verifier",
        os.getenv("GMAIL_RECIPIENT", "verify@example.com"),
    )
    await set_policy(TENANT, POLICY_THRESHOLD, "INR")
    return await create_ingest_key(TENANT)


async def trace_id_for(application_id: str) -> str | None:
    statement = select(TelemetrySpan.trace_id).where(
        TelemetrySpan.tenant_id == TENANT,
        TelemetrySpan.attributes["loan.application_id"].astext
        == application_id,
    )
    async with get_db_connection() as session:
        return await session.scalar(statement)


async def trace_state(trace_id: str) -> dict[str, Any]:
    async with get_db_connection() as session:
        span_count = await session.scalar(
            select(func.count())
            .select_from(TelemetrySpan)
            .where(
                TelemetrySpan.tenant_id == TENANT,
                TelemetrySpan.trace_id == trace_id,
            )
        )
        violations = list(
            (
                await session.scalars(
                    select(GovernanceViolation).where(
                        GovernanceViolation.tenant_id == TENANT,
                        GovernanceViolation.trace_id == trace_id,
                    )
                )
            ).all()
        )
        audits = list(
            (
                await session.scalars(
                    select(AgentAuditLog).where(
                        AgentAuditLog.agent_name == "truvanai_monitor",
                        AgentAuditLog.parameters["tenant_id"].astext
                        == TENANT,
                        AgentAuditLog.parameters["trace_id"].astext
                        == trace_id,
                    )
                )
            ).all()
        )
    return {
        "spans": int(span_count or 0),
        "violations": violations,
        "audits": audits,
    }


async def spans_by_application(application_id: str) -> list[dict[str, Any]]:
    statement = select(TelemetrySpan).where(
        TelemetrySpan.tenant_id == TENANT,
        TelemetrySpan.attributes["loan.application_id"].astext
        == application_id,
    )
    async with get_db_connection() as session:
        rows = list((await session.scalars(statement)).all())
    return [
        {
            "tenant_id": row.tenant_id,
            "trace_id": row.trace_id,
            "name": row.name,
            "attributes": row.attributes or {},
        }
        for row in rows
    ]


async def all_violations() -> list[GovernanceViolation]:
    async with get_db_connection() as session:
        return list(
            (
                await session.scalars(
                    select(GovernanceViolation).where(
                        GovernanceViolation.tenant_id == TENANT
                    )
                )
            ).all()
        )


async def count_audit_rows() -> int:
    statement = select(func.count()).select_from(AgentAuditLog).where(
        AgentAuditLog.agent_name == "truvanai_monitor",
        AgentAuditLog.parameters["tenant_id"].astext == TENANT,
    )
    async with get_db_connection() as session:
        return int((await session.scalar(statement)) or 0)


async def evaluate_again(trace_id: str) -> dict[str, Any]:
    from agents.telemetry_monitor import evaluate_traces

    return await evaluate_traces(TENANT, [trace_id])


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def violation_by_rule(state: dict[str, Any], prefix: str) -> Any:
    return next(
        (
            row
            for row in state["violations"]
            if str(row.rule).startswith(prefix)
        ),
        None,
    )


def audit_by_action(state: dict[str, Any], action: str) -> Any:
    return next(
        (row for row in state["audits"] if row.action_name == action),
        None,
    )


def check_static_imports() -> None:
    forbidden = {"shared", "tools", "agents", "hitl", "ingest"}
    problems: list[str] = []
    for path in sorted((PROJECT_ROOT / "bank_system").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
        bad = sorted(modules & forbidden)
        if bad:
            problems.append(f"{path.name}: {', '.join(bad)}")
    record(
        14,
        "bank_system imports no TruvanAI modules",
        not problems,
        "; ".join(problems),
    )


def check_existing_verifiers() -> None:
    candidates = [
        path
        for path in (PROJECT_ROOT / "scripts").glob("*.py")
        if path.name != Path(__file__).name
        and any(
            word in path.stem.lower()
            for word in ("verify", "gate", "graph")
        )
    ]
    if not candidates:
        record(
            17,
            "existing gate and graph verifiers still pass",
            True,
            "no existing verifier scripts found",
            skipped=True,
        )
        return

    failures: list[str] = []
    for path in candidates:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            failures.append(f"{path.name}: exit {completed.returncode}")
    record(
        17,
        "existing gate and graph verifiers still pass",
        not failures,
        "; ".join(failures) or f"{len(candidates)} verifier(s) passed",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    port = free_port()
    server: ServerThread | None = None
    key: str | None = None

    try:
        import hitl.dashboard as dashboard_module

        server = ServerThread(dashboard_module.app, port)
        server.start()
        print(f"Verifier server started on port {port}")

        server.run(cleanup_tenant_data())
        key = server.run(create_verify_tenant())
        print(f"Temporary tenant {TENANT} is ready with policy 1000 INR")
        print()

        if not key:
            raise RuntimeError("The verifier could not create an ingest key.")

        base_url = f"http://127.0.0.1:{port}"
        password = os.getenv("HITL_PASSWORD", "").strip()
        valid_json = otlp_json(
            [
                otlp_span(
                    hex_id(32),
                    hex_id(16),
                    "loan.process",
                    "VERIFY-AUTH",
                    500.0,
                )
            ]
        )

        with httpx.Client(base_url=base_url, timeout=30) as client:
            # 1. Missing key.
            response = post_traces(client, None, valid_json)
            record(
                1,
                "missing ingest key returns 401",
                response.status_code == 401,
                str(response.status_code),
            )

            # 2. Bad key.
            response = post_traces(client, "not-a-real-key", valid_json)
            record(
                2,
                "bad ingest key returns 401",
                response.status_code == 401,
                str(response.status_code),
            )

            # 3. Malformed body.
            response = post_traces(
                client,
                key,
                b"{this is not valid json",
                "application/json",
            )
            record(
                3,
                "malformed body returns 400",
                response.status_code == 400,
                str(response.status_code),
            )

            # 4. Oversized body.
            try:
                status = oversized_status(port, key)
            except Exception as exc:
                status = None
                detail = f"{type(exc).__name__}: {exc}"
            else:
                detail = str(status)
            record(
                4,
                "oversized body returns 413",
                status == 413,
                detail,
            )

            # 5. 500 auto-approved is compliant.
            application = f"VERIFY-500-{hex_id(6)}"
            completed = run_bank(port, key, "500", application, "10000")
            state = server.run(trace_state(server.run(trace_id_for(application))))
            compliance = audit_by_action(state, "governance_check_passed")
            record(
                5,
                "500 auto-approved is ALLOWED / YES",
                completed.returncode == 0
                and len(state["violations"]) == 0
                and compliance is not None
                and compliance.policy_status == "ALLOWED"
                and compliance.verdict_card == "YES",
                (
                    f"exit {completed.returncode}, "
                    f"{len(state['violations'])} violation(s), "
                    f"compliant={compliance is not None}"
                ),
            )

            # 6. 1000 exactly is compliant.
            application = f"VERIFY-1000-{hex_id(6)}"
            completed = run_bank(port, key, "1000", application, "10000")
            state = server.run(trace_state(server.run(trace_id_for(application))))
            compliance = audit_by_action(state, "governance_check_passed")
            record(
                6,
                "1000 exactly is compliant",
                completed.returncode == 0
                and len(state["violations"]) == 0
                and compliance is not None,
                (
                    f"exit {completed.returncode}, "
                    f"{len(state['violations'])} violation(s)"
                ),
            )

            # 7. 1000.01 auto-approved is R1.
            application = f"VERIFY-100001-{hex_id(6)}"
            completed = run_bank(port, key, "1000.01", application, "10000")
            state = server.run(trace_state(server.run(trace_id_for(application))))
            r1 = violation_by_rule(state, "R1")
            audit = audit_by_action(state, "governance_violation_detected")
            record(
                7,
                "1000.01 auto-approved is R1 BLOCKED / NO",
                completed.returncode == 0
                and r1 is not None
                and audit is not None
                and audit.policy_status == "BLOCKED"
                and audit.verdict_card == "NO",
                (
                    f"exit {completed.returncode}, "
                    f"r1={getattr(r1, 'rule', None)}, "
                    f"audit={getattr(audit, 'policy_status', None)}/"
                    f"{getattr(audit, 'verdict_card', None)}"
                ),
            )

            # 8. 2000 with bank threshold 10000, policy threshold wins.
            application = f"VERIFY-2000-{hex_id(6)}"
            completed = run_bank(port, key, "2000", application, "10000")
            state = server.run(trace_state(server.run(trace_id_for(application))))
            r1 = violation_by_rule(state, "R1")
            audit = audit_by_action(state, "governance_violation_detected")
            audit_threshold = None
            if audit is not None and isinstance(audit.parameters, dict):
                audit_threshold = audit.parameters.get("threshold")
            record(
                8,
                "2000 uses policy threshold 1000, not bank 10000",
                completed.returncode == 0
                and r1 is not None
                and float(r1.threshold or 0) == POLICY_THRESHOLD
                and audit_threshold is not None
                and float(audit_threshold) == POLICY_THRESHOLD,
                (
                    f"violation={getattr(r1, 'threshold', None)}, "
                    f"audit={audit_threshold}"
                ),
            )

            # 9. 1500 with bank threshold 1000 is sent to review.
            application = f"VERIFY-1500-{hex_id(6)}"
            completed = run_bank(port, key, "1500", application, "1000")
            state = server.run(trace_state(server.run(trace_id_for(application))))
            compliance = audit_by_action(state, "governance_check_passed")
            record(
                9,
                "1500 sent to human review is ALLOWED / YES",
                completed.returncode == 0
                and "SENT_TO_HITL" in completed.stdout
                and len(state["violations"]) == 0
                and compliance is not None,
                (
                    f"exit {completed.returncode}, "
                    f"sent={'SENT_TO_HITL' in completed.stdout}, "
                    f"{len(state['violations'])} violation(s)"
                ),
            )

            # 10. Same spans sent twice are idempotent.
            trace_id = hex_id(32)
            application = f"VERIFY-DUP-{hex_id(6)}"
            duplicate_payload = otlp_json(
                [
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "loan.process",
                        application,
                        2000.0,
                    ),
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "agent.tool.auto_approve",
                        application,
                        2000.0,
                        "AUTO_APPROVED",
                    ),
                ]
            )
            first = post_traces(client, key, duplicate_payload)
            second = post_traces(client, key, duplicate_payload)
            state = server.run(trace_state(trace_id))
            summary = server.run(evaluate_again(trace_id))
            again = server.run(trace_state(trace_id))
            record(
                10,
                "duplicate spans write no second row or alert",
                first.status_code == 200
                and second.status_code == 200
                and state["spans"] == 2
                and len(state["violations"]) == 1
                and len(state["audits"]) == 1
                and summary["violations_detected"] == 0
                and again["spans"] == 2
                and len(again["violations"]) == 1
                and len(again["audits"]) == 1,
                (
                    f"spans={state['spans']}, "
                    f"violations={len(state['violations'])}, "
                    f"audits={len(state['audits'])}, "
                    f"reeval={summary['violations_detected']}"
                ),
            )

            # 11. A spoofed tenant attribute is ignored.
            trace_id = hex_id(32)
            application = f"VERIFY-SPOOF-{hex_id(6)}"
            spoofed_payload = otlp_json(
                [
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "agent.tool.auto_approve",
                        application,
                        500.0,
                        "AUTO_APPROVED",
                        extra_attributes=[
                            {
                                "key": "tenant_id",
                                "value": {"stringValue": "spoofed-tenant"},
                            },
                            {
                                "key": "tenant.id",
                                "value": {"stringValue": "spoofed-tenant"},
                            },
                        ],
                    )
                ]
            )
            response = post_traces(client, key, spoofed_payload)
            spans = server.run(spans_by_application(application))
            record(
                11,
                "spoofed tenant attribute is ignored",
                response.status_code == 200
                and len(spans) == 1
                and all(
                    row["tenant_id"] == TENANT for row in spans
                ),
                (
                    f"status={response.status_code}, "
                    f"tenants={sorted({row['tenant_id'] for row in spans})}"
                ),
            )

            # 12. Inconsistent amounts are R2.
            trace_id = hex_id(32)
            application = f"VERIFY-R2-{hex_id(6)}"
            r2_payload = otlp_json(
                [
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "loan.process",
                        application,
                        1500.0,
                    ),
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "agent.tool.auto_approve",
                        application,
                        500.0,
                        "AUTO_APPROVED",
                    ),
                ]
            )
            response = post_traces(client, key, r2_payload)
            time.sleep(0.2)
            state = server.run(trace_state(trace_id))
            r2 = violation_by_rule(state, "R2")
            record(
                12,
                "inconsistent amounts are R2",
                response.status_code == 200 and r2 is not None,
                f"status={response.status_code}, rule={getattr(r2, 'rule', None)}",
            )

            # 13. Missing amount is R0 PENDING / UNKNOWN.
            trace_id = hex_id(32)
            application = f"VERIFY-R0-{hex_id(6)}"
            r0_payload = otlp_json(
                [
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "loan.process",
                        application,
                        2000.0,
                    ),
                    otlp_span(
                        trace_id,
                        hex_id(16),
                        "agent.tool.auto_approve",
                        application,
                        None,
                        "AUTO_APPROVED",
                    ),
                ]
            )
            response = post_traces(client, key, r0_payload)
            time.sleep(0.2)
            state = server.run(trace_state(trace_id))
            r0 = violation_by_rule(state, "R0")
            r0_audit = audit_by_action(state, "governance_violation_detected")
            record(
                13,
                "missing amount is PENDING / UNKNOWN",
                response.status_code == 200
                and r0 is not None
                and r0_audit is not None
                and r0_audit.policy_status == "PENDING"
                and r0_audit.verdict_card == "UNKNOWN",
                (
                    f"rule={getattr(r0, 'rule', None)}, "
                    f"audit={getattr(r0_audit, 'policy_status', None)}/"
                    f"{getattr(r0_audit, 'verdict_card', None)}"
                ),
            )

            # 14. Static import isolation.
            check_static_imports()

            # 15. Auth domains are separate.
            if password:
                dashboard_response = client.get(
                    "/api/governance/violations",
                    headers={"x-truvanai-api-key": key},
                )
                reverse_response = client.post(
                    "/v1/traces",
                    content=valid_json,
                    headers={"content-type": "application/json"},
                    auth=("truvanai", password),
                )
                record(
                    15,
                    "ingest key and dashboard password are rejected across domains",
                    dashboard_response.status_code == 401
                    and reverse_response.status_code == 401,
                    (
                        f"dashboard={dashboard_response.status_code}, "
                        f"ingest={reverse_response.status_code}"
                    ),
                )
            else:
                record(
                    15,
                    "ingest key and dashboard password are rejected across domains",
                    False,
                    "HITL_PASSWORD is not set",
                )

            # 16. ALERTS_ENABLED=false records disabled.
            violations = server.run(all_violations())
            record(
                16,
                "ALERTS_ENABLED=false records alert_status disabled",
                bool(violations)
                and all(
                    row.alert_status == "disabled" for row in violations
                ),
                (
                    f"{len(violations)} violation(s), statuses="
                    f"{sorted({row.alert_status for row in violations})}"
                ),
            )

        # 18. Containment callback (Group F).
        import bank_system.control as bank_control_module
        from bank_system.store import get_loan, init_store

        init_store()
        bank_control_port = free_port()
        bank_control_config = uvicorn.Config(
            bank_control_module.app,
            host="127.0.0.1",
            port=bank_control_port,
            log_level="warning",
            lifespan="off",
        )
        bank_control_server = uvicorn.Server(bank_control_config)
        bank_control_thread = threading.Thread(
            target=bank_control_server.run, daemon=True
        )
        bank_control_thread.start()
        for _ in range(200):
            if bank_control_server.started:
                break
            time.sleep(0.05)

        server.run(set_tenant_control_url(
            f"http://127.0.0.1:{bank_control_port}"
        ))

        application = f"VERIFY-CONTAIN-{hex_id(6)}"
        env = os.environ.copy()
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = base_url
        env["OTEL_EXPORTER_OTLP_HEADERS"] = f"x-truvanai-api-key={key}"
        env["BANK_APPROVAL_THRESHOLD"] = "10000"
        env["ALERTS_ENABLED"] = "false"
        env["BANK_CONTROL_SECRET"] = "verify-containment-secret"

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "bank_system.run",
                "--amount",
                "2000",
                "--id",
                application,
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        time.sleep(1.0)

        state = server.run(trace_state(server.run(trace_id_for(application))))
        r1 = violation_by_rule(state, "R1")
        loan = get_loan(application)
        containment_audit = next(
            (
                audit
                for audit in state["audits"]
                if audit.action_name == "containment_action"
            ),
            None,
        )
        record(
            18,
            "containment callback succeeds for R1",
            completed.returncode == 0
            and r1 is not None
            and r1.containment_status == "succeeded"
            and loan is not None
            and loan["status"] == "ON_HOLD_FOR_REVIEW"
            and containment_audit is not None,
            (
                f"exit={completed.returncode}, "
                f"containment={getattr(r1, 'containment_status', None)}, "
                f"loan={loan['status'] if loan else None}, "
                f"audit={containment_audit is not None}"
            ),
        )

        bank_control_server.should_exit = True
        bank_control_thread.join(timeout=10)

        # 17. Existing verifiers.
        check_existing_verifiers()

    except Exception as exc:
        traceback.print_exc()
        record(0, "verifier run", False, f"{type(exc).__name__}: {exc}")
    finally:
        if server is not None and server.loop is not None:
            try:
                audit_count = server.run(count_audit_rows())
                server.run(cleanup_tenant_data())
                from shared.db import dispose_engine

                server.run(dispose_engine())
                print()
                print(
                    f"Temporary tenant data removed. "
                    f"{audit_count} audit row(s) retained for {TENANT}."
                )
            except Exception:
                traceback.print_exc()
            server.stop()

    print()
    print("=" * 96)
    print(f"{'#':<4}{'Check':<62}{'Result':<8}Detail")
    print("-" * 96)
    for number, name, status, detail in sorted(RESULTS):
        print(f"{number:<4}{name[:60]:<62}{status:<8}{detail}")
    print("=" * 96)

    passed = sum(1 for _, _, status, _ in RESULTS if status == "PASS")
    failed = sum(1 for _, _, status, _ in RESULTS if status == "FAIL")
    skipped = sum(1 for _, _, status, _ in RESULTS if status == "SKIP")
    print(
        f"Results: {passed} passed, {failed} failed, {skipped} skipped"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
