"""Async PostgreSQL access and audit-trail helpers for TruvanAI.

This module defines the SQLAlchemy model for the permanent agent audit log,
creates the async engine used by the rest of the application, and provides
the database helpers exported to agents and the HITL dashboard.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.policy import (
    APPROVED,
    AUTO_APPROVED,
    BLOCKED,
    PENDING_HITL,
    REJECTED,
    SUBMITTED,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

POLICY_STATUSES = frozenset({"ALLOWED", "BLOCKED", "PENDING"})
VERDICT_CARDS = frozenset({"YES", "NO", "UNKNOWN"})
LOAN_STATUSES = frozenset(
    {
        SUBMITTED,
        PENDING_HITL,
        AUTO_APPROVED,
        APPROVED,
        REJECTED,
        BLOCKED,
    }
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """Declarative base for all database models."""


class AgentAuditLog(Base):
    """Permanent record of one action attempted by an AI agent."""

    __tablename__ = "agent_audit_log"
    __table_args__ = (
        CheckConstraint(
            "policy_status IN ('ALLOWED', 'BLOCKED', 'PENDING')",
            name="ck_agent_audit_log_policy_status",
        ),
        CheckConstraint(
            "verdict_card IN ('YES', 'NO', 'UNKNOWN')",
            name="ck_agent_audit_log_verdict_card",
        ),
        Index("ix_agent_audit_log_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(120), nullable=False)
    action_name: Mapped[str] = mapped_column(String(120), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    policy_status: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict_card: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="UNKNOWN",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class LoanApplication(Base):
    """Trusted loan application record used by the preflight policy gate."""

    __tablename__ = "loan_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('SUBMITTED', 'PENDING_HITL', 'AUTO_APPROVED', "
            "'APPROVED', 'REJECTED', 'BLOCKED')",
            name="ck_loan_applications_status",
        ),
        CheckConstraint(
            "amount >= 0",
            name="ck_loan_applications_amount_nonnegative",
        ),
        Index("ix_loan_applications_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        unique=True,
    )
    customer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="SUBMITTED",
    )
    request_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    risk_flags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def _database_url() -> str:
    """Return DATABASE_URL normalized for SQLAlchemy's asyncpg driver."""
    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add a PostgreSQL connection string to .env."
        )

    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw_url.startswith("postgresql+asyncpg://"):
        return raw_url

    raise RuntimeError(
        "DATABASE_URL must start with postgresql://, postgres://, "
        "or postgresql+asyncpg://."
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            _database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_db_connection() -> AsyncIterator[AsyncSession]:
    """Yield an async database session and close it when the context exits.

    Callers are responsible for committing write transactions. The session is
    always closed, which safely rolls back any transaction that was not
    committed.
    """
    session = _get_session_factory()()
    try:
        yield session
    finally:
        await session.close()


async def initialize_database() -> None:
    """Create the audit and trusted loan tables if they do not exist."""
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Dispose database connections and reset the module-level engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def log_action(
    agent_name: str,
    action_name: str,
    parameters: Mapping[str, Any] | None = None,
    result: str = "",
    policy_status: str = "ALLOWED",
    verdict_card: str = "UNKNOWN",
) -> int:
    """Insert one agent action into the audit trail and return its row id.

    Parameters are persisted as JSONB so the monitor can inspect exactly what
    an agent attempted. Policy status and verdict values are validated before
    the insert so invalid records never enter the permanent audit trail.
    """
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("agent_name must be a non-empty string.")
    if not isinstance(action_name, str) or not action_name.strip():
        raise ValueError("action_name must be a non-empty string.")
    if not isinstance(result, str):
        raise TypeError("result must be a string.")
    if policy_status not in POLICY_STATUSES:
        allowed = ", ".join(sorted(POLICY_STATUSES))
        raise ValueError(f"policy_status must be one of: {allowed}.")
    if verdict_card not in VERDICT_CARDS:
        allowed = ", ".join(sorted(VERDICT_CARDS))
        raise ValueError(f"verdict_card must be one of: {allowed}.")
    if parameters is not None and not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping or None.")

    entry = AgentAuditLog(
        agent_name=agent_name.strip(),
        action_name=action_name.strip(),
        parameters=dict(parameters or {}),
        result=result,
        policy_status=policy_status,
        verdict_card=verdict_card,
    )

    async with get_db_connection() as session:
        session.add(entry)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return entry.id


async def get_recent_actions(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent audit actions, newest first, as JSON-ready dictionaries."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 1:
        raise ValueError("limit must be at least 1.")

    statement = (
        select(AgentAuditLog)
        .order_by(AgentAuditLog.timestamp.desc(), AgentAuditLog.id.desc())
        .limit(limit)
    )

    async with get_db_connection() as session:
        rows = (await session.scalars(statement)).all()

    return [_audit_row_to_dict(row) for row in rows]


def _audit_row_to_dict(row: AgentAuditLog) -> dict[str, Any]:
    """Convert one audit ORM row into the JSON-ready shape used by the API."""
    return {
        "id": row.id,
        "agent_name": row.agent_name,
        "action_name": row.action_name,
        "parameters": row.parameters,
        "result": row.result,
        "policy_status": row.policy_status,
        "verdict_card": row.verdict_card,
        "timestamp": row.timestamp.isoformat(),
    }


async def get_actions_by_ids(
    action_ids: Iterable[int],
) -> list[dict[str, Any]]:
    """Return specific audit actions by ID, newest first.

    The webhook monitor uses this to evaluate exactly the event it received
    instead of every row in the recent audit window.
    """
    clean_ids: list[int] = []
    for action_id in action_ids:
        if isinstance(action_id, bool) or not isinstance(action_id, int):
            raise TypeError("action_ids must contain only integers.")
        if action_id < 1:
            raise ValueError("action_ids must contain positive integers.")
        if action_id not in clean_ids:
            clean_ids.append(action_id)

    if not clean_ids:
        return []

    statement = (
        select(AgentAuditLog)
        .where(AgentAuditLog.id.in_(clean_ids))
        .order_by(AgentAuditLog.timestamp.desc(), AgentAuditLog.id.desc())
    )

    async with get_db_connection() as session:
        rows = (await session.scalars(statement)).all()

    return [_audit_row_to_dict(row) for row in rows]


async def get_audit_log_table_name() -> str | None:
    """Return the audit table name when it exists in the public schema."""
    async with get_engine().connect() as connection:
        value = await connection.scalar(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name = 'agent_audit_log'"
            )
        )
    return str(value) if value else None


async def get_loan_table_name() -> str | None:
    """Return the trusted loan table name when it exists."""
    async with get_engine().connect() as connection:
        value = await connection.scalar(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name = 'loan_applications'"
            )
        )
    return str(value) if value else None


async def get_loan_table_columns() -> set[str]:
    """Return the column names present on the trusted loan table."""
    async with get_engine().connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'loan_applications'"
            )
        )
    return {str(row[0]) for row in rows}
