"""Single relational model for the whole company.

Tables are prefixed by domain (``commerce_``, ``ledger_``, ``company_``, ``registry_``, ``ops_``,
``memory_``) instead of using Postgres schemas, so the identical model runs on SQLite (default) and
Postgres (docker-compose) without migrations diverging.
"""

from __future__ import annotations

import json
import logging
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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
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
    status: Mapped[str] = mapped_column(String, default="evaluating")  # evaluating | provisioning | active | rejected | degraded | retired
    credential_env: Mapped[str | None] = mapped_column(String, nullable=True)  # name of the env var holding its secret
    notes: Mapped[str] = mapped_column(Text, default="")
    # E5/E6: provisioning checklist [{step, done}] and the last credential check (never the credential itself)
    checklist: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # E5 reminders
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
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # e.g. Lemon Squeezy variant id
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
    # I14: marketing email only with recorded opt-in; unsubscribing is permanent until a new opt-in
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    marketing_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    marketing_consent_source: Mapped[str | None] = mapped_column(String, nullable=True)  # checkout | form | import
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Suppression(Base):
    """I14: addresses that must never get marketing email (unsubscribed, bounced, complained), customer or not."""

    __tablename__ = "commerce_suppressions"
    email: Mapped[str] = mapped_column(String, primary_key=True)  # lower-case
    reason: Mapped[str] = mapped_column(String)  # unsubscribe | bounce | complaint | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Order(Base):
    __tablename__ = "commerce_orders"
    # one local order per store-platform order, even if two webhook deliveries race (NULL for mock orders)
    __table_args__ = (Index("uq_commerce_orders_external_id", "external_id", unique=True,
                            sqlite_where=text("external_id IS NOT NULL"), postgresql_where=text("external_id IS NOT NULL")),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ord"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("commerce_customers.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("commerce_products.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount_eur: Mapped[float] = mapped_column(Float)
    vat_eur: Mapped[float] = mapped_column(Float, default=0.0)
    campaign_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="paid")  # paid | delivered | partially_refunded | refunded | failed
    # I4: delivery (key assigned) and the access email are tracked separately, so a failed email is resent
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)  # store platform order id (unique, see above)
    # H8: EU digital content - consent to immediate delivery and acknowledgement that the withdrawal right is lost
    withdrawal_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    withdrawal_consent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    # I13: one stable, expiring download link per order (the token is random; links are re-issued, not re-randomised)
    download_token: Mapped[str | None] = mapped_column(String, nullable=True)
    download_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    # VAT detail (J7): set on vat_payable lines so VAT can be reported per country and rate (OSS)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    vat_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | approved | rejected | executed | failed | expired
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # I10: identical pending escalations share a dedup key; stale ones expire instead of executing late
    dedup_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebhookReceipt(Base):
    """One row per webhook delivery processed, so a provider's retries or duplicates are handled exactly once."""

    __tablename__ = "ops_webhook_receipts"
    __table_args__ = (UniqueConstraint("provider", "event_key", name="uq_webhook_provider_event"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String)  # lemonsqueezy | agentmail
    event_key: Mapped[str] = mapped_column(String)  # provider event / delivery id (or a hash of the body)
    topic: Mapped[str] = mapped_column(String, default="")
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the ops_events row it produced
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Incident(Base):
    """K2/B: one open problem per (monitor, subject), raised by deterministic monitors; alerts are sent from here."""

    __tablename__ = "ops_incidents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("inc"))
    monitor: Mapped[str] = mapped_column(String, index=True)  # e.g. undelivered_order, heartbeat, ledger_imbalance
    severity: Mapped[str] = mapped_column(String)  # P1 | P2 | P3
    subject: Mapped[str] = mapped_column(String, default="")  # what it is about (order id, agent, ...) - dedup key part
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open | acknowledged | resolved
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Report(Base):
    """J3: an immutable report snapshot. Corrections are new versions; numbers come from code (data), text from agents."""

    __tablename__ = "ops_reports"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("rpt"))
    kind: Mapped[str] = mapped_column(String, index=True)  # daily_flash | weekly_review | ...
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ErrorRow(Base):
    """K1: unexpected errors with tracebacks, so humans and the Sentinel can find root causes."""

    __tablename__ = "ops_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    component: Mapped[str] = mapped_column(String, index=True)  # agent | gate | event_bus | api | scheduler
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exc_type: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sentry_event_id: Mapped[str | None] = mapped_column(String, nullable=True)


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
    # at-least-once delivery: see aecom.bus.events.drain
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # lease / retry time
    done_handlers: Mapped[list[str]] = mapped_column(JSON, default=list)  # handlers that already succeeded
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead: Mapped[bool] = mapped_column(Boolean, default=False)  # gave up after max attempts → dead letter


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
    cost_booked: Mapped[bool] = mapped_column(Boolean, default=False)  # sub-cent costs are booked in batches
    web_searches: Mapped[int] = mapped_column(Integer, default=0)  # D4: server-side tool uses, billed per use
    web_fetches: Mapped[int] = mapped_column(Integer, default=0)
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
    # D3: shortened, masked arguments and result, so a run can be read later without LangSmith
    args_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # I15 provenance: who wrote it and from what; shared memories wait for review before other agents recall them
    source_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tainted: Mapped[bool] = mapped_column(Boolean, default=False)  # written in a run that read untrusted content
    status: Mapped[str] = mapped_column(String, default="active")  # active | pending_review | rejected
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
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


def _alembic_config(connection: Any = None) -> Any:
    """Alembic config located relative to the repo root; ``connection`` makes migrations run on our engine."""
    from alembic.config import Config

    from aecom.config import ROOT

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def init_db() -> None:
    """Bring the database to the current schema.

    * empty database -> ``alembic upgrade head``
    * tables but no ``alembic_version`` (installs from before C4) -> create missing tables and run the old
      ``_add_missing_columns`` stopgap once, then ``alembic stamp head``
    * already versioned -> ``alembic upgrade head`` (no-op when current)
    """
    from alembic import command
    from sqlalchemy import inspect

    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    legacy = bool(tables) and "alembic_version" not in tables
    if legacy:
        Base.metadata.create_all(engine)
        _add_missing_columns()
        _align_legacy_schema()
    with engine.begin() as conn:
        cfg = _alembic_config(conn)
        if legacy:
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")


# Legacy path only (pre-Alembic databases, see init_db). New schema changes and data fixes go into Alembic
# revisions under migrations/versions/ from now on; do not extend _BACKFILL or _add_missing_columns.
# One-time data fixes for columns added to an existing table: (table, column) -> SQL run right after ADD COLUMN.
_BACKFILL = {
    # orders delivered before email tracking existed already got their email; don't resend to all of them
    ("commerce_orders", "email_sent"):
        "UPDATE commerce_orders SET email_sent = 1 WHERE status IN ('delivered', 'partially_refunded', 'refunded')",
}


def _align_legacy_schema() -> None:
    """Make a pre-Alembic database match the models before it is stamped: create missing indexes and make
    columns that ``_add_missing_columns`` added as nullable NOT NULL where the model says so (NULLs get the
    column default first). Without this the database would be stamped at head yet still differ from it."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    engine = get_engine()
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
        diffs = [d[0] for d in compare_metadata(ctx, Base.metadata) if isinstance(d, list)]
        to_fix: dict[str, list[str]] = {}
        for op_name, _schema, table_name, column_name, _info, _old, new in diffs:
            if op_name == "modify_nullable" and new is False:
                to_fix.setdefault(table_name, []).append(column_name)
        ops = Operations(ctx)
        for table_name, columns in to_fix.items():
            table = Base.metadata.tables[table_name]
            for name in columns:
                default = table.c[name].default.arg if table.c[name].default is not None else None
                if callable(default):
                    default = default(None) if default.__code__.co_argcount else default()
                if default is not None:
                    conn.execute(text(f"UPDATE {table_name} SET {name} = :v WHERE {name} IS NULL"),
                                 {"v": default if not isinstance(default, (list, dict)) else json.dumps(default)})
            try:  # best effort: rebuilding the table must never make `aecom init` fail on real data
                with conn.begin_nested(), ops.batch_alter_table(table_name) as batch:
                    for name in columns:
                        batch.alter_column(name, existing_type=table.c[name].type, nullable=False)
            except Exception as exc:
                logging.getLogger(__name__).warning("could not make %s.%s NOT NULL (left nullable): %s",
                                                    table_name, columns, exc)


def _add_missing_columns() -> None:
    """Pre-Alembic stopgap, now only run once for unversioned databases (see init_db): create_all never alters
    existing tables, so add new nullable or defaulted columns to databases created by an older version."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                if isinstance(default, bool):
                    ddl += f" DEFAULT {int(default) if engine.dialect.name == 'sqlite' else str(default).upper()}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))
                if (backfill := _BACKFILL.get((table.name, col.name))):
                    conn.execute(text(backfill))


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
