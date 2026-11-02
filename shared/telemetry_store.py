"""Telemetry storage and tenant management for TruvanAI OpenTelemetry integration.

This module owns every database operation for the OTel pipeline:
  - tenants and their policies
  - ingest keys (raw keys are never stored, only SHA-256 hashes)
  - incoming spans from the OTLP receiver
  - governance violations detected by the telemetry monitor

The tables are created idempotently by scripts/init_db.py. All helpers
are async and use the same SQLAlchemy engine that the rest of TruvanAI uses.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base, get_db_connection, get_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

_RAW_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class Tenant(Base):
    """A bank or other customer that sends telemetry to TruvanAI."""

    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False)
    control_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TenantPolicy(Base):
    """Per-tenant governance policy (approval threshold and currency)."""

    __tablename__ = "tenant_policies"

    tenant_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    approval_threshold: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class IngestKey(Base):
    """An API key that authenticates OTLP span delivery for a tenant.

    The raw key is returned once at creation and never stored. Only the
    SHA-256 hex digest is persisted.
    """

    __tablename__ = "ingest_keys"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class TelemetrySpan(Base):
    """One OTel span received from a tenant's agent."""

    __tablename__ = "telemetry_spans"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "trace_id", "span_id",
            name="uq_telemetry_spans_tenant_trace_span",
        ),
        Index("ix_telemetry_spans_tenant_evaluated", "tenant_id", "evaluated"),
        Index("ix_telemetry_spans_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    evaluated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class GovernanceViolation(Base):
    """A policy violation detected by the telemetry monitor for one trace+rule."""

    __tablename__ = "governance_violations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "trace_id", "rule",
            name="uq_governance_violations_tenant_trace_rule",
        ),
        Index("ix_governance_violations_tenant_detected", "tenant_id", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    rule: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    application_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    alert_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    alert_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    containment_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_configured"
    )


# ---------------------------------------------------------------------------
# Table helpers used by init_db.py
# ---------------------------------------------------------------------------


async def initialize_telemetry_tables() -> None:
    """Create the five OTel tables idempotently."""
    async with get_engine().begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    Tenant.__table__,
                    TenantPolicy.__table__,
                    IngestKey.__table__,
                    TelemetrySpan.__table__,
                    GovernanceViolation.__table__,
                ],
                checkfirst=True,
            )
        )


async def get_telemetry_table_names() -> set[str]:
    """Return which OTel table names exist in the public schema."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ("
                "'tenants', 'tenant_policies', 'ingest_keys', "
                "'telemetry_spans', 'governance_violations')"
            )
        )
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------------------
# Tenant helpers
# ---------------------------------------------------------------------------


async def create_tenant(
    tenant_id: str,
    display_name: str,
    owner_email: str,
    control_url: str | None = None,
) -> None:
    """Insert a tenant record. Raises ValueError when the id already exists."""
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string.")
    if not display_name or not display_name.strip():
        raise ValueError("display_name must be a non-empty string.")
    if not owner_email or not owner_email.strip():
        raise ValueError("owner_email must be a non-empty string.")

    row = Tenant(
        tenant_id=tenant_id.strip(),
        display_name=display_name.strip(),
        owner_email=owner_email.strip(),
        control_url=control_url,
    )
    async with get_db_connection() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(
                f"Tenant '{tenant_id}' already exists."
            ) from exc


async def get_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Return a tenant as a plain dict or None when not found."""
    stmt = select(Tenant).where(Tenant.tenant_id == tenant_id.strip())
    async with get_db_connection() as session:
        row = await session.scalar(stmt)
    if row is None:
        return None
    return {
        "tenant_id": row.tenant_id,
        "display_name": row.display_name,
        "owner_email": row.owner_email,
        "control_url": row.control_url,
        "created_at": row.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


async def set_policy(
    tenant_id: str,
    approval_threshold: float | Decimal,
    currency: str = "INR",
) -> None:
    """Upsert a tenant's governance policy."""
    threshold = Decimal(str(approval_threshold))
    if threshold < 0:
        raise ValueError("approval_threshold cannot be negative.")
    currency_clean = currency.strip().upper()
    if not currency_clean:
        raise ValueError("currency must be a non-empty string.")

    async with get_db_connection() as session:
        existing = await session.scalar(
            select(TenantPolicy).where(TenantPolicy.tenant_id == tenant_id)
        )
        if existing is None:
            session.add(
                TenantPolicy(
                    tenant_id=tenant_id,
                    approval_threshold=threshold,
                    currency=currency_clean,
                )
            )
        else:
            existing.approval_threshold = threshold
            existing.currency = currency_clean
            existing.updated_at = datetime.now(timezone.utc)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(
                f"Tenant '{tenant_id}' not found."
            ) from exc


async def get_policy(tenant_id: str) -> dict[str, Any] | None:
    """Return a tenant's policy or None when not configured."""
    stmt = select(TenantPolicy).where(TenantPolicy.tenant_id == tenant_id)
    async with get_db_connection() as session:
        row = await session.scalar(stmt)
    if row is None:
        return None
    return {
        "tenant_id": row.tenant_id,
        "approval_threshold": float(row.approval_threshold),
        "currency": row.currency,
        "updated_at": row.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Ingest key helpers
# ---------------------------------------------------------------------------


def _hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw ingest key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def create_ingest_key(tenant_id: str) -> str:
    """Create an ingest key for a tenant and return the raw key once.

    The raw key is NEVER stored. Only the SHA-256 hash is persisted.
    The caller must print or transmit the returned value immediately.
    """
    raw_key = secrets.token_urlsafe(_RAW_KEY_BYTES)
    key_hash = _hash_key(raw_key)

    row = IngestKey(tenant_id=tenant_id, key_hash=key_hash)
    async with get_db_connection() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(
                f"Key collision for tenant '{tenant_id}'. This is extremely "
                "unlikely. Try again."
            ) from exc

    return raw_key


async def resolve_tenant_from_key(raw_key: str) -> str | None:
    """Return the tenant_id for a valid, non-revoked key, or None.

    Returns None when the key is missing, unknown, or revoked.
    """
    if not raw_key or not raw_key.strip():
        return None
    key_hash = _hash_key(raw_key.strip())
    stmt = select(IngestKey).where(
        IngestKey.key_hash == key_hash,
        IngestKey.revoked.is_(False),
    )
    async with get_db_connection() as session:
        row = await session.scalar(stmt)
    if row is None:
        return None
    return row.tenant_id


# ---------------------------------------------------------------------------
# Span storage helpers
# ---------------------------------------------------------------------------


async def store_spans(
    tenant_id: str,
    spans: list[dict[str, Any]],
) -> int:
    """Insert spans with ON CONFLICT DO NOTHING. Returns the inserted count.

    Each span is inserted individually inside its own savepoint so a duplicate
    does not abort the whole batch. This gives true ON CONFLICT DO NOTHING
    semantics without a dialect-specific INSERT statement.
    """
    if not spans:
        return 0

    inserted = 0
    async with get_db_connection() as session:
        async with session.begin():
            for span_data in spans:
                row = TelemetrySpan(
                    tenant_id=tenant_id,
                    trace_id=span_data["trace_id"],
                    span_id=span_data["span_id"],
                    parent_span_id=span_data.get("parent_span_id"),
                    name=span_data["name"],
                    service_name=span_data.get("service_name", "unknown"),
                    start_time=span_data.get("start_time"),
                    end_time=span_data.get("end_time"),
                    attributes=span_data.get("attributes", {}),
                    evaluated=False,
                )
                try:
                    async with session.begin_nested():
                        session.add(row)
                        await session.flush()
                    inserted += 1
                except IntegrityError:
                    # Duplicate (tenant_id, trace_id, span_id) - skip silently.
                    pass

    return inserted


async def get_trace_spans(
    tenant_id: str,
    trace_ids: list[str],
) -> list[dict[str, Any]]:
    """Return all spans for the given tenant and trace IDs."""
    if not trace_ids:
        return []
    stmt = (
        select(TelemetrySpan)
        .where(
            TelemetrySpan.tenant_id == tenant_id,
            TelemetrySpan.trace_id.in_(trace_ids),
        )
        .order_by(TelemetrySpan.start_time, TelemetrySpan.id)
    )
    async with get_db_connection() as session:
        rows = (await session.scalars(stmt)).all()
    return [_span_to_dict(row) for row in rows]


def _span_to_dict(row: TelemetrySpan) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "parent_span_id": row.parent_span_id,
        "name": row.name,
        "service_name": row.service_name,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "end_time": row.end_time.isoformat() if row.end_time else None,
        "attributes": row.attributes or {},
        "received_at": row.received_at.isoformat(),
        "evaluated": row.evaluated,
    }


async def mark_evaluated(tenant_id: str, trace_ids: list[str]) -> int:
    """Mark all spans in the given traces as evaluated. Returns updated count."""
    if not trace_ids:
        return 0
    stmt = (
        update(TelemetrySpan)
        .where(
            TelemetrySpan.tenant_id == tenant_id,
            TelemetrySpan.trace_id.in_(trace_ids),
            TelemetrySpan.evaluated.is_(False),
        )
        .values(evaluated=True)
        .returning(TelemetrySpan.id)
    )
    async with get_db_connection() as session:
        rows = (await session.scalars(stmt)).all()
        await session.commit()
    return len(list(rows))


async def get_unevaluated_trace_ids(tenant_id: str) -> list[str]:
    """Return distinct trace IDs that have at least one unevaluated span."""
    stmt = (
        select(TelemetrySpan.trace_id)
        .where(
            TelemetrySpan.tenant_id == tenant_id,
            TelemetrySpan.evaluated.is_(False),
        )
        .distinct()
    )
    async with get_db_connection() as session:
        rows = (await session.scalars(stmt)).all()
    return list(rows)


# ---------------------------------------------------------------------------
# Violation helpers
# ---------------------------------------------------------------------------


async def record_violation(
    tenant_id: str,
    trace_id: str,
    span_id: str,
    rule: str,
    severity: str,
    reason: str,
    application_id: str | None = None,
    amount: float | Decimal | None = None,
    threshold: float | Decimal | None = None,
    alert_status: str = "pending",
) -> int | None:
    """Insert a governance violation. Returns the row id or None on duplicate."""
    amount_dec = Decimal(str(amount)) if amount is not None else None
    threshold_dec = Decimal(str(threshold)) if threshold is not None else None

    row = GovernanceViolation(
        tenant_id=tenant_id,
        trace_id=trace_id,
        span_id=span_id,
        rule=rule,
        severity=severity,
        application_id=application_id,
        amount=amount_dec,
        threshold=threshold_dec,
        reason=reason,
        alert_status=alert_status,
    )
    async with get_db_connection() as session:
        session.add(row)
        try:
            await session.commit()
            return row.id
        except IntegrityError:
            await session.rollback()
            return None


async def update_violation_alert(
    violation_id: int,
    alert_status: str,
    alert_error: str | None = None,
) -> None:
    """Update the alert delivery status of an existing violation row."""
    stmt = (
        update(GovernanceViolation)
        .where(GovernanceViolation.id == violation_id)
        .values(alert_status=alert_status, alert_error=alert_error)
    )
    async with get_db_connection() as session:
        await session.execute(stmt)
        await session.commit()


async def list_recent_violations(
    limit: int = 20,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent governance violations, newest first."""
    stmt = select(GovernanceViolation).order_by(
        GovernanceViolation.detected_at.desc(),
        GovernanceViolation.id.desc(),
    )
    if tenant_id:
        stmt = stmt.where(GovernanceViolation.tenant_id == tenant_id)
    stmt = stmt.limit(min(limit, 100))
    async with get_db_connection() as session:
        rows = (await session.scalars(stmt)).all()
    return [_violation_to_dict(row) for row in rows]


def _violation_to_dict(row: GovernanceViolation) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "rule": row.rule,
        "severity": row.severity,
        "application_id": row.application_id,
        "amount": float(row.amount) if row.amount is not None else None,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "reason": row.reason,
        "detected_at": row.detected_at.isoformat(),
        "alert_status": row.alert_status,
        "alert_error": row.alert_error,
        "containment_status": row.containment_status,
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def telemetry_stats(tenant_id: str | None = None) -> dict[str, Any]:
    """Return high-level telemetry counts for the dashboard stats API."""
    # Use ORM queries to avoid SQL injection.
    from sqlalchemy import func as sqlfunc

    async with get_db_connection() as session:
        span_q = select(sqlfunc.count()).select_from(TelemetrySpan)
        trace_q = select(
            sqlfunc.count(TelemetrySpan.trace_id.distinct())
        ).select_from(TelemetrySpan)
        viol_q = select(sqlfunc.count()).select_from(GovernanceViolation)

        if tenant_id:
            span_q = span_q.where(TelemetrySpan.tenant_id == tenant_id)
            trace_q = trace_q.where(TelemetrySpan.tenant_id == tenant_id)
            viol_q = viol_q.where(GovernanceViolation.tenant_id == tenant_id)

        spans_count = (await session.scalar(span_q)) or 0
        traces_count = (await session.scalar(trace_q)) or 0
        violations_count = (await session.scalar(viol_q)) or 0

    return {
        "spans_received": int(spans_count),
        "traces": int(traces_count),
        "violations": int(violations_count),
    }
