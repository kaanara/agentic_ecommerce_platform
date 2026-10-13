"""Single relational model for the whole company.

Tables are prefixed by domain (``commerce_``, ``ledger_``, ``company_``, ``registry_``, ``ops_``,
``memory_``) instead of using Postgres schemas, so the identical model runs on SQLite (default) and
Postgres (docker-compose) without migrations diverging.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from aecom.config import get_settings


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------- company
class Okr(Base):
    __tablename__ = "company_okrs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("okr"))
    pillar: Mapped[str] = mapped_column(String)  # financial | customer | operational
    objective: Mapped[str] = mapped_column(Text)
    key_result: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(String)
    target: Mapped[float] = mapped_column(Float)
    current: Mapped[float] = mapped_column(Float, default=0.0)
    quarter: Mapped[str] = mapped_column(String, default="2026-Q4")
    owner: Mapped[str] = mapped_column(String, default="ceo")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Decision(Base):
    """Architecture/business decision records (ADRs). Mirrored to docs/decisions/*.md."""

    __tablename__ = "company_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("adr"))
    category: Mapped[str] = mapped_column(String)  # ecommerce_platform | payment | ad_platform | social | mcp | vendor | strategy
    title: Mapped[str] = mapped_column(String)
    choice: Mapped[str] = mapped_column(String)
    rationale: Mapped[str] = mapped_column(Text)
    alternatives: Mapped[list[Any]] = mapped_column(JSON, default=list)
    scorecard: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str] = mapped_column(String, default="ceo")
    status: Mapped[str] = mapped_column(String, default="accepted")  # proposed | accepted | superseded
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Vendor(Base):
    __tablename__ = "company_vendors"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ven"))
    name: Mapped[str] = mapped_column(String, unique=True)
    category: Mapped[str] = mapped_column(String)  # platform | payment | ads | social | email | asset_supplier | api
    monthly_cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="evaluating")  # evaluating | provisioning | active | degraded | retired
    credential_env: Mapped[str | None] = mapped_column(String, nullable=True)  # name of the env var holding its secret
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Campaign(Base):
    __tablename__ = "company_campaigns"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("cmp"))
    name: Mapped[str] = mapped_column(String)
    channel: Mapped[str] = mapped_column(String)  # google_ads | meta_ads | tiktok | linkedin | newsletter | organic_social
    product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    budget_eur: Mapped[float] = mapped_column(Float, default=0.0)
    spent_eur: Mapped[float] = mapped_column(Float, default=0.0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    revenue_eur: Mapped[float] = mapped_column(Float, default=0.0)
    copy: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | live | paused | ended
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- commerce
class Product(Base):
    __tablename__ = "commerce_products"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("prd"))
    sku: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String, default="license")  # license | download | course | subscription
    price_eur: Mapped[float] = mapped_column(Float)
    unit_cost_eur: Mapped[float] = mapped_column(Float, default=0.0)  # wholesale licence cost / royalty
    asset_url: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LicenseKey(Base):
    __tablename__ = "commerce_license_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("lic"))
    product_id: Mapped[str] = mapped_column(ForeignKey("commerce_products.id"))
    key: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="available")  # available | assigned | revoked
    order_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Customer(Base):
    __tablename__ = "commerce_customers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("cus"))
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str] = mapped_column(String, default="DE")
    acquisition_channel: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Order(Base):
    __tablename__ = "commerce_orders"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ord"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("commerce_customers.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("commerce_products.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount_eur: Mapped[float] = mapped_column(Float)
    vat_eur: Mapped[float] = mapped_column(Float, default=0.0)
    campaign_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="paid")  # paid | delivered | refunded | failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Refund(Base):
    __tablename__ = "commerce_refunds"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ref"))
    order_id: Mapped[str] = mapped_column(ForeignKey("commerce_orders.id"))
    amount_eur: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- ledger
class LedgerEntry(Base):
    """Double-entry bookkeeping. Every transaction has >= 2 rows whose debits equal credits."""

    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)  # cash | revenue | vat_payable | cogs | marketing_expense | ai_expense | vendor_expense | refunds
    debit: Mapped[float] = mapped_column(Float, default=0.0)
    credit: Mapped[float] = mapped_column(Float, default=0.0)
    memo: Mapped[str] = mapped_column(Text, default="")
    ref: Mapped[str | None] = mapped_column(String, nullable=True)  # order / campaign / vendor id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- registry
class RegistryTool(Base):
    """Tools, skills and MCP servers the company can use, plus their lifecycle."""

    __tablename__ = "registry_tools"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)  # builtin | mcp_server | skill
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String, default="1")
    status: Mapped[str] = mapped_column(String, default="approved")  # proposed | sandboxed | approved | deprecated
    side_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # mcp launch config / skill body
    proposed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Allowlist(Base):
    __tablename__ = "registry_allowlist"
    __table_args__ = (UniqueConstraint("agent_id", "tool_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    tool_name: Mapped[str] = mapped_column(String)


# --------------------------------------------------------------------------- ops
class Approval(Base):
    """Human-in-the-loop queue. The approved action is stored verbatim and executed exactly as approved."""

    __tablename__ = "ops_approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("apr"))
    agent_id: Mapped[str] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String)
    args: Mapped[dict[str, Any]] = mapped_column(JSON)
    risk_tier: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | approved | rejected | executed | failed
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Append-only (enforced by a SQLAlchemy hook below)."""

    __tablename__ = "ops_audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    actor: Mapped[str] = mapped_column(String)  # agent id | human:<name> | system
    action: Mapped[str] = mapped_column(String)
    decision: Mapped[str] = mapped_column(String)  # allow | deny | escalate | executed | error | info
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)


class EventRow(Base):
    __tablename__ = "ops_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    topic: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String, default="system")
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentRun(Base):
    __tablename__ = "ops_agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("run"))
    run_id: Mapped[str] = mapped_column(String, index=True)  # graph-level run / thread id
    agent_id: Mapped[str] = mapped_column(String, index=True)
    model: Mapped[str] = mapped_column(String)
    task: Mapped[str] = mapped_column(Text)
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="running")  # running | ok | error | vetoed
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ToolCallLog(Base):
    __tablename__ = "ops_tool_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    tool_name: Mapped[str] = mapped_column(String, index=True)
    decision: Mapped[str] = mapped_column(String)  # allow | deny | escalate
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class OutboxEmail(Base):
    __tablename__ = "ops_outbox_emails"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("eml"))
    direction: Mapped[str] = mapped_column(String, default="out")  # out | in
    inbox: Mapped[str] = mapped_column(String)
    to: Mapped[str] = mapped_column(String)
    sender: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String, default="mock")  # agentmail | mock
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "ops_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# --------------------------------------------------------------------------- memory
class Memory(Base):
    """Long-term semantic memory. On Postgres, add a pgvector column; SQLite uses keyword recall."""

    __tablename__ = "memory_items"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("mem"))
    agent_id: Mapped[str] = mapped_column(String, index=True)  # or "shared"
    namespace: Mapped[str] = mapped_column(String, default="facts")
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Lesson(Base):
    """Procedural memory: proposed edits to an agent's AGENTS.md, gated by Judge + human."""

    __tablename__ = "memory_lessons"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("les"))
    agent_id: Mapped[str] = mapped_column(String)
    lesson: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="proposed")  # proposed | applied | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- engine
_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            from pathlib import Path

            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        sqlite = url.startswith("sqlite")
        _engine = create_engine(url, json_serializer=lambda o: json.dumps(o, default=str),
                                connect_args={"timeout": 30, "check_same_thread": False} if sqlite else {})
        if sqlite:
            @event.listens_for(_engine, "connect")
            def _pragmas(conn: Any, _rec: Any) -> None:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine(url: str | None = None) -> None:
    """Used by tests to point at a fresh database."""
    global _engine, _Session
    if url:
        get_settings().database_url = url
    _engine, _Session = None, None


def init_db() -> None:
    Base.metadata.create_all(get_engine())


@contextmanager
def session() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@event.listens_for(Session, "before_flush")
def _audit_is_append_only(sess: Session, _ctx: Any, _instances: Any) -> None:
    for obj in list(sess.dirty) + list(sess.deleted):
        if isinstance(obj, AuditLog):
            raise PermissionError("ops_audit_log is append-only")
