"""Creating dinners and managing who is at them."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import Session, func, select

from app.models import Allocation, Dinner, Participant, Payment, utcnow
from app.security import sha256
from app.services.events import audit, touch
from app.services.references import issue_reference, next_dinner_code

MAX_NAME = 40
MAX_PEOPLE = 60


def clean_name(raw: str) -> str:
    name = " ".join(str(raw or "").split())[:MAX_NAME]
    if not name:
        raise HTTPException(422, "Enter a name so people know who you are.")
    return name


def clean_table(raw) -> str:
    """A table number or name as the restaurant calls it: "12", "Courtyard 3"."""
    text = " ".join(str(raw or "").split())[:20]
    return text[6:].strip() if text.lower().startswith("table ") else text


def create_dinner(
    s: Session, restaurant_name: str, organiser_name: str, *, is_demo: bool = False, table_label: str = ""
) -> Dinner:
    dinner = Dinner(
        code=next_dinner_code(s),
        restaurant_name=" ".join(str(restaurant_name or "").split())[:80],
        table_label=clean_table(table_label),
        is_demo=is_demo,
    )
    s.add(dinner)
    s.flush()
    me = add_participant(s, dinner, organiser_name, is_organiser=True)
    dinner.organiser_participant_id = me.id
    audit(s, dinner.id, "organiser", f"Created dinner {dinner.code}")
    touch(s, dinner)
    return dinner


def add_participant(s: Session, dinner: Dinner, name: str, *, is_organiser: bool = False) -> Participant:
    name = clean_name(name)
    count = s.exec(select(func.count()).select_from(Participant).where(Participant.dinner_id == dinner.id)).one()
    if count >= MAX_PEOPLE:
        raise HTTPException(409, "This dinner is full.")
    # Duplicate display names are fine; the reference and id keep people apart.
    p = Participant(
        dinner_id=dinner.id,
        display_name=name,
        reference=issue_reference(s, dinner.code, name),
        is_organiser=is_organiser,
        sort=count,
    )
    s.add(p)
    s.flush()
    audit(s, dinner.id, "organiser" if is_organiser else name, f"{name} joined ({p.reference})")
    touch(s, dinner)
    return p


def rename(s: Session, dinner: Dinner, p: Participant, name: str) -> None:
    p.display_name = clean_name(name)  # reference deliberately unchanged
    s.add(p)
    touch(s, dinner)


def remove(s: Session, dinner: Dinner, p: Participant) -> None:
    if p.is_organiser:
        raise HTTPException(409, "You can't remove yourself.")
    if s.exec(select(Allocation).where(Allocation.participant_id == p.id)).first():
        raise HTTPException(409, f"{p.display_name} still has items. Move or remove them first.")
    if s.exec(select(Payment).where(Payment.participant_id == p.id, Payment.undone_at == None)).first():  # noqa: E711
        raise HTTPException(409, f"{p.display_name} has payments recorded.")
    p.removed = True
    p.session_hash = None
    s.add(p)
    audit(s, dinner.id, "organiser", f"Removed {p.display_name}")
    touch(s, dinner)


def issue_recovery(s: Session, p: Participant) -> str:
    token = secrets.token_urlsafe(24)
    p.recovery_hash = sha256(token)
    p.recovery_expires_at = utcnow() + timedelta(hours=12)
    s.add(p)
    return token


def redeem_recovery(s: Session, dinner: Dinner, token: str) -> Participant:
    p = s.exec(
        select(Participant).where(Participant.recovery_hash == sha256(token), Participant.dinner_id == dinner.id)
    ).first()
    if p is None or p.removed or not p.recovery_expires_at or p.recovery_expires_at < utcnow():
        raise HTTPException(404, "This personal link has expired or was already used. Ask the organiser for a new one.")
    p.recovery_hash = None
    p.recovery_expires_at = None
    s.add(p)
    return p
