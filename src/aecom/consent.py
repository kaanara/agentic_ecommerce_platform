"""Marketing consent (I14): newsletters only reach customers who opted in and never unsubscribed, bounced or complained.

* ``record_opt_in`` stores proof of consent on the customer (when, from where) and lifts an earlier unsubscribe.
  A bounce, complaint or manual suppression is **not** lifted: removing it is a human decision (see
  docs/wiki/GDPR-and-Retention.md, "Marketing consent").
* ``unsubscribe`` withdraws consent and adds a ``Suppression`` row (also for addresses that are not customers).
* ``suppress`` records a delivery problem (bounce / complaint) or a manual block.
* ``is_marketable`` is the single check every marketing send must pass.

Addresses are compared lower-case. Every change is audited with the customer id only, never the address.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aecom.context import email_address
from aecom.db import AuditLog, Customer, Suppression, now, session

REASONS = ("unsubscribe", "bounce", "complaint", "manual")
HARD_REASONS = ("bounce", "complaint", "manual")  # only a human removes these


def normalize(email: str) -> str:
    return email_address(email) or (email or "").strip().lower()


def _customer(s: Session, email: str) -> Customer | None:
    return s.scalars(select(Customer).where(func.lower(Customer.email) == email)).first()


def _audit(s: Session, action: str, customer: Customer | None, **detail: Any) -> None:
    s.add(AuditLog(actor="system", action=action, decision="executed",
                   detail={"customer_id": customer.id if customer else None, **detail}))


def _suppress(s: Session, email: str, reason: str) -> bool:
    """Add or harden the suppression row. True if something changed."""
    if reason not in REASONS:
        raise ValueError(f"unknown suppression reason {reason!r}; one of {', '.join(REASONS)}")
    row = s.get(Suppression, email)
    if row is None:
        s.add(Suppression(email=email, reason=reason))
        return True
    if reason in HARD_REASONS and row.reason not in HARD_REASONS:  # a bounce outranks an unsubscribe
        row.reason = reason
        return True
    return False


def record_opt_in(email: str, source: str) -> dict[str, Any]:
    """Record a marketing opt-in (source: checkout | form | ...). Creates the customer record if needed.
    Refused (ValueError) while the address is suppressed for bounce, complaint or manual block."""
    addr = normalize(email)
    if not addr:
        raise ValueError("no email address")
    with session() as s:
        sup = s.get(Suppression, addr)
        if sup is not None and sup.reason in HARD_REASONS:
            raise ValueError(f"address is suppressed ({sup.reason}); only a human can remove that suppression")
        c = _customer(s, addr)
        if c is None:
            c = Customer(email=addr)
            s.add(c)
            s.flush()
        if sup is not None:  # a new opt-in after an unsubscribe
            s.delete(sup)
        c.marketing_consent, c.marketing_consent_at, c.marketing_consent_source = True, now(), source
        c.unsubscribed_at = None
        _audit(s, "marketing_opt_in", c, source=source)
        return {"customer_id": c.id, "marketing_consent": True, "source": source}


def unsubscribe(email: str, reason: str = "unsubscribe") -> dict[str, Any]:
    """Withdraw consent and suppress the address. Idempotent: a repeat changes (and audits) nothing."""
    addr = normalize(email)
    if not addr:
        raise ValueError("no email address")
    with session() as s:
        c = _customer(s, addr)
        changed = _suppress(s, addr, reason)
        if c is not None and (c.marketing_consent or c.unsubscribed_at is None):
            c.marketing_consent, c.unsubscribed_at = False, now()
            changed = True
        if changed:
            _audit(s, "marketing_unsubscribe", c, reason=reason)
        return {"customer_id": c.id if c else None, "unsubscribed": True, "changed": changed}


def suppress(email: str, reason: str, *, db: Session | None = None) -> dict[str, Any]:
    """Never send marketing email to this address again (bounce | complaint | manual | unsubscribe).
    With ``db`` the change commits atomically with the caller's transaction."""
    addr = normalize(email)
    if not addr:
        raise ValueError("no email address")

    def run(s: Session) -> dict[str, Any]:
        c = _customer(s, addr)
        changed = _suppress(s, addr, reason)
        if changed:
            _audit(s, "marketing_suppressed", c, reason=reason)
        return {"customer_id": c.id if c else None, "reason": reason, "changed": changed}

    if db is not None:
        return run(db)
    with session() as s:
        return run(s)


def is_marketable(email: str, *, db: Session | None = None) -> bool:
    """True only for a customer with recorded consent, not unsubscribed, and not suppressed."""
    addr = normalize(email)
    if not addr:
        return False

    def check(s: Session) -> bool:
        c = _customer(s, addr)
        if c is None or not c.marketing_consent or c.unsubscribed_at is not None:
            return False
        return s.get(Suppression, addr) is None

    if db is not None:
        return check(db)
    with session() as s:
        return check(s)
