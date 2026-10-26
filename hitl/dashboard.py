"""FastAPI HITL dashboard for TruvanAI loan approvals.

The service reads pending loan approvals from Redis, persists human decisions
to PostgreSQL, and broadcasts dashboard state to connected WebSocket clients.
HTTP routes require Basic authentication with HITL_PASSWORD.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from redis.asyncio import Redis

from agents.loan_agent import run_loan_application
from shared.db import get_recent_actions, log_action
from shared.loan_store import (
    IssuedLoanRequest,
    get_loan_application,
    get_recent_auto_approved_loans,
    issue_loan_request,
    resolve_hitl_loan,
    restore_hitl_loan,
)
from shared.policy import (
    HITL_APPROVED_QUEUE,
    HITL_QUEUE,
    HITL_REJECTED_QUEUE,
    HUMAN_APPROVAL_THRESHOLD,
    MAX_LOAN_AMOUNT,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

PENDING_QUEUE = HITL_QUEUE
APPROVED_QUEUE = HITL_APPROVED_QUEUE
REJECTED_QUEUE = HITL_REJECTED_QUEUE
SESSION_COOKIE = "truvanai_hitl_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
WATCH_INTERVAL_SECONDS = 1.0
MAX_LOAN_AMOUNT_DECIMAL = Decimal(str(MAX_LOAN_AMOUNT))
CENT = Decimal("0.01")


class LoanSubmission(BaseModel):
    """A user-defined loan amount submitted through the dashboard."""

    amount: Decimal

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        """Accept a positive amount with no more than two decimal places."""
        if not value.is_finite() or value <= 0:
            raise ValueError("Enter a loan amount greater than zero.")
        if value > MAX_LOAN_AMOUNT_DECIMAL:
            raise ValueError(
                f"Loan amount cannot exceed {MAX_LOAN_AMOUNT_DECIMAL:,.2f}."
            )
        try:
            quantized = value.quantize(CENT)
        except InvalidOperation as exc:
            raise ValueError("Use no more than two decimal places.") from exc
        if quantized != value:
            raise ValueError("Use no more than two decimal places.")
        return quantized


logger = logging.getLogger("truvanai.hitl")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
basic_auth = HTTPBasic(auto_error=False)
_redis_client: Redis | None = None


class ConnectionManager:
    """Track WebSocket clients and serialize outbound broadcasts."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._state_lock = asyncio.Lock()
        self._broadcast_lock = asyncio.Lock()

    @property
    def has_connections(self) -> bool:
        return bool(self._connections)

    async def register(self, websocket: WebSocket) -> None:
        async with self._state_lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._state_lock:
            self._connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._broadcast_lock:
            for websocket in tuple(self._connections):
                try:
                    await websocket.send_json(payload)
                except Exception:
                    await self.disconnect(websocket)


manager = ConnectionManager()


def get_redis_client() -> Redis:
    """Return the process-wide async Redis client."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not redis_url:
            raise RuntimeError(
                "REDIS_URL is not set. Add a Redis connection string to .env."
            )
        _redis_client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _redis_client


async def close_redis_client() -> None:
    """Close the shared Redis client."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def _hitl_password() -> str:
    """Return the configured dashboard password or fail clearly."""
    password = os.getenv("HITL_PASSWORD", "")
    if not password:
        raise RuntimeError("HITL_PASSWORD is not set in the environment.")
    return password


def _session_signature(expires_at: int, password: str) -> str:
    message = f"truvanai-hitl-session:{expires_at}".encode("utf-8")
    return hmac.new(
        password.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def _create_session_token(password: str) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE_SECONDS
    signature = _session_signature(expires_at, password)
    return f"{expires_at}.{signature}"


def _session_token_is_valid(token: str | None, password: str) -> bool:
    if not token:
        return False
    try:
        expires_text, supplied_signature = token.split(".", 1)
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _session_signature(expires_at, password)
    return secrets.compare_digest(supplied_signature, expected_signature)


def _password_matches(supplied: str | None, expected: str) -> bool:
    if supplied is None:
        return False
    return secrets.compare_digest(
        supplied.encode("utf-8"),
        expected.encode("utf-8"),
    )


async def require_hitl_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(basic_auth),
) -> None:
    """Protect HTTP routes with Basic authentication or a signed session."""
    try:
        password = _hitl_password()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    session_token = request.cookies.get(SESSION_COOKIE)
    if _session_token_is_valid(session_token, password):
        return
    if credentials is not None and _password_matches(credentials.password, password):
        return

    raise HTTPException(
        status_code=401,
        detail="Authentication required.",
        headers={
            "WWW-Authenticate": 'Basic realm="TruvanAI HITL Dashboard", charset="UTF-8"'
        },
    )


def _basic_header_is_valid(header_value: str | None, password: str) -> bool:
    if not header_value:
        return False
    try:
        scheme, encoded_credentials = header_value.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(
            encoded_credentials.strip(),
            validate=True,
        ).decode("utf-8")
        _, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return _password_matches(supplied_password, password)


def _parse_queue_items(raw_items: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        item = json.loads(raw_item)
        if not isinstance(item, dict):
            raise ValueError("Redis queue item is not a JSON object.")
        items.append(item)
    return items


async def _build_dashboard_state() -> dict[str, Any]:
    """Read pending, resolved, automatic approvals, and recent audit data."""
    client = get_redis_client()
    pending_raw, approved_raw, rejected_raw, audit, auto_approved = (
        await asyncio.gather(
            client.lrange(PENDING_QUEUE, 0, -1),
            client.lrange(APPROVED_QUEUE, 0, -1),
            client.lrange(REJECTED_QUEUE, 0, -1),
            get_recent_actions(limit=10),
            get_recent_auto_approved_loans(limit=10),
        )
    )

    approved = _parse_queue_items(approved_raw)
    seen_ids: set[str] = set()
    for item in approved:
        item["resolution"] = "human"
        application_id = item.get("application_id")
        if isinstance(application_id, str) and application_id:
            seen_ids.add(application_id)
    for item in auto_approved:
        if item["application_id"] in seen_ids:
            continue
        approved.append(item)
        seen_ids.add(item["application_id"])

    return {
        "pending": _parse_queue_items(pending_raw),
        "approved": approved,
        "rejected": _parse_queue_items(rejected_raw),
        "audit": audit,
    }


def _new_web_application_id() -> str:
    """Return a unique application identifier for a dashboard submission."""
    date_stamp = time.strftime("%Y%m%d", time.gmtime())
    return f"WEB-{date_stamp}-{secrets.token_hex(4).upper()}"


def _serialize_loan_submission(
    issued: IssuedLoanRequest,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return safe submission fields without the raw request token."""
    status = str(result.get("status") or issued.status)
    human_required = bool(result.get("human_required", False))
    amount_display = f"{issued.amount:,.2f}"
    threshold_display = f"{HUMAN_APPROVAL_THRESHOLD:,.2f}"

    if status == "approved":
        resolution = "automatic"
        message = (
            f"Loan {issued.application_id} for ${amount_display} was "
            f"auto-approved because the amount is at or below "
            f"${threshold_display}."
        )
    elif status == "pending":
        resolution = "human_review"
        message = (
            f"Loan {issued.application_id} for ${amount_display} requires "
            f"human review because the amount is above ${threshold_display}."
        )
    else:
        resolution = "blocked"
        message = (
            f"Loan {issued.application_id} for ${amount_display} was blocked "
            "by the preflight gate."
        )

    payload: dict[str, Any] = {
        "application_id": issued.application_id,
        "amount": issued.amount,
        "status": status,
        "human_required": human_required,
        "resolution": resolution,
        "approval_threshold": HUMAN_APPROVAL_THRESHOLD,
        "message": message,
    }
    blocked_reason = result.get("blocked_reason")
    if status == "blocked" and isinstance(blocked_reason, str):
        payload["blocked_reason"] = blocked_reason
    return payload


MOVE_ITEM_SCRIPT = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
for index = 1, #items do
    local raw_item = items[index]
    local item = cjson.decode(raw_item)
    if item['application_id'] == ARGV[1] then
        redis.call('LREM', KEYS[1], 1, raw_item)
        item['status'] = ARGV[2]
        item['resolved_at'] = ARGV[3]
        local resolved_item = cjson.encode(item)
        redis.call('RPUSH', KEYS[2], resolved_item)
        return resolved_item
    end
end
return nil
"""


async def _restore_pending_item(
    client: Redis,
    target_queue: str,
    resolved_raw: str,
    item: dict[str, Any],
) -> None:
    """Return a resolved queue item to the pending queue after a failure."""
    restored_item = dict(item)
    restored_item["status"] = "pending"
    restored_item.pop("resolved_at", None)
    await client.lrem(target_queue, 1, resolved_raw)
    await client.rpush(
        PENDING_QUEUE,
        json.dumps(restored_item, separators=(",", ":")),
    )


async def _resolve_application(
    application_id: str,
    decision: str,
) -> dict[str, Any]:
    """Move a pending application, update trusted state, and audit the decision."""
    if decision == "approve":
        target_queue = APPROVED_QUEUE
        resolved_status = "approved"
        action_name = "approve_loan"
        policy_status = "ALLOWED"
        verdict_card = "YES"
    elif decision == "reject":
        target_queue = REJECTED_QUEUE
        resolved_status = "rejected"
        action_name = "reject_loan"
        policy_status = "BLOCKED"
        verdict_card = "NO"
    else:
        raise ValueError(f"Unsupported decision: {decision}")

    clean_application_id = application_id.strip()
    if not clean_application_id:
        raise HTTPException(status_code=400, detail="Application ID is required.")

    resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    client = get_redis_client()

    try:
        resolved_raw = await client.eval(
            MOVE_ITEM_SCRIPT,
            2,
            PENDING_QUEUE,
            target_queue,
            clean_application_id,
            resolved_status,
            resolved_at,
        )
    except Exception as exc:
        logger.exception("Failed to move application %s in Redis.", clean_application_id)
        raise HTTPException(
            status_code=503,
            detail="The approval queue is temporarily unavailable.",
        ) from exc

    if resolved_raw is None:
        raise HTTPException(
            status_code=404,
            detail="Loan application is not pending.",
        )

    item = json.loads(resolved_raw)
    if not isinstance(item, dict):
        raise HTTPException(
            status_code=500,
            detail="The resolved queue item is invalid.",
        )

    loan_status_updated = False
    try:
        loan_status_updated = await resolve_hitl_loan(
            clean_application_id,
            decision,
        )
    except Exception as exc:
        logger.exception(
            "Failed to update the trusted loan record for %s.",
            clean_application_id,
        )
        try:
            await _restore_pending_item(
                client,
                target_queue,
                resolved_raw,
                item,
            )
        except Exception:
            logger.exception(
                "Failed to restore application %s to the pending queue.",
                clean_application_id,
            )
        raise HTTPException(
            status_code=503,
            detail="The trusted loan record could not be updated.",
        ) from exc

    confirmation = (
        f"Human operator {resolved_status} loan application "
        f"{clean_application_id}."
    )
    try:
        trusted_record = await get_loan_application(clean_application_id)
        trusted_amount = (
            trusted_record["amount"]
            if trusted_record is not None
            else item.get("amount")
        )
        await log_action(
            agent_name="hitl_dashboard",
            action_name=action_name,
            parameters={
                "application_id": clean_application_id,
                "amount": trusted_amount,
                "amount_source": (
                    "loan_applications"
                    if trusted_record is not None
                    else "hitl_queue"
                ),
                "decision": decision,
                "source_queue": PENDING_QUEUE,
                "target_queue": target_queue,
                "requested_at": item.get("requested_at"),
                "trusted_loan_updated": loan_status_updated,
            },
            result=confirmation,
            policy_status=policy_status,
            verdict_card=verdict_card,
        )
    except Exception as exc:
        logger.exception(
            "Failed to write human decision to the audit trail for %s.",
            clean_application_id,
        )
        if loan_status_updated:
            try:
                restored = await restore_hitl_loan(clean_application_id)
                if not restored:
                    logger.error(
                        "Trusted loan record for %s was not restored.",
                        clean_application_id,
                    )
            except Exception:
                logger.exception(
                    "Failed to restore the trusted loan record for %s.",
                    clean_application_id,
                )
        try:
            await _restore_pending_item(
                client,
                target_queue,
                resolved_raw,
                item,
            )
        except Exception:
            logger.exception(
                "Failed to restore application %s to the pending queue.",
                clean_application_id,
            )
        raise HTTPException(
            status_code=503,
            detail="The human decision could not be recorded in the audit trail.",
        ) from exc

    await _broadcast_state_safely()
    return {
        "application_id": clean_application_id,
        "status": resolved_status,
        "item": item,
    }


async def _broadcast_state_safely() -> None:
    if not manager.has_connections:
        return
    try:
        state = await _build_dashboard_state()
        await manager.broadcast({"type": "state", "data": state})
    except Exception:
        logger.exception("Failed to broadcast dashboard state.")


def _state_signature(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


async def _watch_for_changes() -> None:
    """Detect live queue and audit changes, then notify every WebSocket client."""
    previous_signature: str | None = None
    while True:
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)
        if not manager.has_connections:
            previous_signature = None
            continue
        try:
            state = await _build_dashboard_state()
            signature = _state_signature(state)
            if signature != previous_signature:
                await manager.broadcast({"type": "state", "data": state})
                previous_signature = signature
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to refresh dashboard state.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    watcher = asyncio.create_task(
        _watch_for_changes(),
        name="hitl-dashboard-watcher",
    )
    try:
        yield
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        await close_redis_client()


app = FastAPI(
    title="TruvanAI HITL Dashboard",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Return a lightweight liveness response for deployment health checks."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _auth: None = Depends(require_hitl_auth),
) -> HTMLResponse:
    """Serve the self-contained dashboard application."""
    try:
        password = _hitl_password()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "approval_threshold_display": f"{HUMAN_APPROVAL_THRESHOLD:,.2f}",
            "max_loan_amount": MAX_LOAN_AMOUNT,
            "max_loan_amount_display": f"{MAX_LOAN_AMOUNT:,.2f}",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    forwarded_proto = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    )
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_create_session_token(password),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=is_https,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/queue")
async def get_pending_queue(
    _auth: None = Depends(require_hitl_auth),
) -> list[dict[str, Any]]:
    """Return all pending loan approvals from the Redis queue."""
    try:
        client = get_redis_client()
        raw_items = await client.lrange(PENDING_QUEUE, 0, -1)
        return _parse_queue_items(raw_items)
    except Exception as exc:
        logger.exception("Failed to read the pending approval queue.")
        raise HTTPException(
            status_code=503,
            detail="The approval queue is temporarily unavailable.",
        ) from exc


@app.get("/api/state")
async def get_dashboard_state(
    _auth: None = Depends(require_hitl_auth),
) -> dict[str, Any]:
    """Return pending, approved, rejected, and recent audit data."""
    try:
        return await _build_dashboard_state()
    except Exception as exc:
        logger.exception("Failed to build dashboard state.")
        raise HTTPException(
            status_code=503,
            detail="Dashboard data is temporarily unavailable.",
        ) from exc


@app.post("/api/loans", status_code=201)
async def submit_loan(
    submission: LoanSubmission,
    _auth: None = Depends(require_hitl_auth),
) -> dict[str, Any]:
    """Create a trusted loan request and route it through the loan graph."""
    application_id = _new_web_application_id()
    try:
        issued = await issue_loan_request(
            application_id=application_id,
            customer_id=f"CUST-{application_id}",
            amount=float(submission.amount),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("Rejected loan submission: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="The loan request could not be created.",
        ) from exc
    except Exception as exc:
        logger.exception("Failed to create a trusted loan request.")
        raise HTTPException(
            status_code=503,
            detail="The trusted loan store is temporarily unavailable.",
        ) from exc

    try:
        result = await run_loan_application(
            application_id=issued.application_id,
            request_token=issued.request_token,
        )
    except Exception as exc:
        logger.exception(
            "Loan %s was created but the loan graph failed.",
            issued.application_id,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Loan {issued.application_id} was created but could not be "
                "processed. Check the audit trail before retrying."
            ),
        ) from exc

    payload = _serialize_loan_submission(issued, result)
    await _broadcast_state_safely()
    return payload


@app.post("/api/approve/{application_id}")
async def approve_application(
    application_id: str,
    _auth: None = Depends(require_hitl_auth),
) -> dict[str, Any]:
    """Approve a pending loan and record the human decision."""
    try:
        return await _resolve_application(application_id, "approve")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to approve application %s.", application_id)
        raise HTTPException(
            status_code=500,
            detail="The approval operation failed.",
        ) from exc


@app.post("/api/reject/{application_id}")
async def reject_application(
    application_id: str,
    _auth: None = Depends(require_hitl_auth),
) -> dict[str, Any]:
    """Reject a pending loan and record the human decision."""
    try:
        return await _resolve_application(application_id, "reject")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to reject application %s.", application_id)
        raise HTTPException(
            status_code=500,
            detail="The rejection operation failed.",
        ) from exc


@app.websocket("/ws")
async def websocket_dashboard(websocket: WebSocket) -> None:
    """Send live dashboard state to authenticated WebSocket clients."""
    try:
        password = _hitl_password()
    except RuntimeError:
        await websocket.close(code=1011)
        return

    session_token = websocket.cookies.get(SESSION_COOKIE)
    authorization_header = websocket.headers.get("authorization")
    authenticated = _session_token_is_valid(
        session_token,
        password,
    ) or _basic_header_is_valid(authorization_header, password)
    if not authenticated:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        initial_state = await _build_dashboard_state()
        await websocket.send_json({"type": "state", "data": initial_state})
    except Exception:
        logger.exception("Failed to send initial WebSocket state.")
        await websocket.close(code=1011)
        return

    await manager.register(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)

