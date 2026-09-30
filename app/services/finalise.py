"""Locking the bill, and unlocking it again safely."""

from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Dinner, Payment, utcnow
from app.services.billing import bill_for
from app.services.events import audit, touch
from app.services.money import fmt


def check_dinner_version(dinner: Dinner, expected: int | None) -> None:
    if expected is None or dinner.version != expected:
        raise HTTPException(409, "The bill changed a moment ago. Showing the latest — check and try again.")


def set_total(s: Session, dinner: Dinner, version: int, cents: int | None, source: str) -> None:
    if dinner.status == "finalised":
        raise HTTPException(423, "Reopen the bill to change the total.")
    check_dinner_version(dinner, version)
    if cents is not None and (cents < 0 or cents > 100_000_00):
        raise HTTPException(422, "That total doesn't look right.")
    dinner.bill_total_cents = cents
    dinner.bill_total_source = source if cents is not None else None
    dinner.version += 1
    audit(s, dinner.id, "organiser", f"Restaurant total set to {fmt(cents)} ({source})")
    touch(s, dinner)


def finalise(s: Session, dinner: Dinner, revision: int) -> dict:
    """Lock the bill exactly as the organiser saw it.

    `revision` is the dinner revision on the organiser's screen. Any change by
    anyone since then — a guest joining a dish a second earlier — refuses the
    finalisation, so the shares locked are the shares that were reviewed.
    """
    if dinner.status == "finalised":
        raise HTTPException(409, "Already finalised.")
    if revision != dinner.revision:
        raise HTTPException(409, "Something changed a moment ago. Check the refreshed bill, then finalise.")
    bill = bill_for(s, dinner)
    if not bill.accounted_for:
        raise HTTPException(409, {"message": "Not everything is accounted for yet.", "issues": bill.issues})
    now = utcnow()
    dinner.status = "finalised"
    dinner.finalised_at = now
    dinner.finalise_count += 1
    dinner.final_shares = {pid: p["total"] for pid, p in bill.people.items()}
    if dinner.instructions_issued_at is None:
        dinner.instructions_issued_at = now
    dinner.payment_window_ends_at = now + timedelta(days=get_settings().payment_window_days)
    dinner.version += 1
    audit(s, dinner.id, "organiser", f"Finalised bill at {fmt(bill.bill_total)} (#{dinner.finalise_count})")
    touch(s, dinner, "finalised")
    return {"finalised": True}


def reopen(s: Session, dinner: Dinner, version: int, acknowledged: bool) -> dict:
    if dinner.status != "finalised":
        raise HTTPException(409, "The bill isn't finalised.")
    check_dinner_version(dinner, version)
    received = s.exec(
        select(Payment).where(Payment.dinner_id == dinner.id, Payment.undone_at == None)  # noqa: E711
    ).all()
    if not acknowledged:
        raise HTTPException(
            409,
            {
                "code": "confirm_reopen",
                "message": (
                    "Payment instructions have already gone out"
                    + (
                        f" and {len(received)} payment(s) totalling "
                        f"{fmt(sum(p.amount_cents for p in received))} have been received"
                        if received
                        else ""
                    )
                    + ". Reopening lets you change the bill; received payments are kept and "
                    "anyone's new balance (or overpayment) will be shown when you finalise again."
                ),
                "payments_received": len(received),
            },
        )
    dinner.status = "open"
    dinner.finalised_at = None
    dinner.version += 1
    audit(s, dinner.id, "organiser", "Reopened the bill")
    touch(s, dinner, "reopened")
    return {"reopened": True}
