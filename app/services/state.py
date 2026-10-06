"""What each screen is allowed to see.

`guest_state` is everything a guest's phone receives. It contains the shared
dinner (menu, orders, allocations, totals) and that guest's *own* reference
and payment status — never anyone else's reference or payment status, never
a bank transaction, and nothing from Up. `organiser_state` adds the rest.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.models import (
    AuditEntry,
    BankTransaction,
    Dinner,
    MenuCategory,
    MenuItem,
    MenuPage,
    Participant,
    Shortlist,
)
from app.services import menu_share, payments, restaurants
from app.services.billing import active_participants, active_receipt, bill_for, line_allocations, live_lines
from app.services.menu import menu_items
from app.services.orders import serialize_line
from app.services.reconcile import serialize_receipt
from app.services.up import get_setting


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def payment_profile(s: Session) -> dict:
    return get_setting(s, "payment_profile", {}) or {}


def _dinner(d: Dinner) -> dict:
    return {
        "id": d.id,
        "code": d.code,
        "restaurant_name": d.restaurant_name,
        "table_label": d.table_label,
        "status": d.status,
        "revision": d.revision,
        "version": d.version,
        "is_demo": d.is_demo,
        "bill_total_cents": d.bill_total_cents,
        "bill_total_source": d.bill_total_source,
        "finalised_at": _iso(d.finalised_at),
        "instructions_issued_at": _iso(d.instructions_issued_at),
        "reopened": d.status == "open" and d.instructions_issued_at is not None,
    }


def _item(i: MenuItem) -> dict:
    return {
        "id": i.id,
        "page_id": i.page_id,
        "category_id": i.category_id,
        "name": i.name,
        "description": i.description,
        "price_cents": i.price_cents,
        "price_text": i.price_text,
        "variants": i.variants,
        "extras": i.extras,
        "labels": i.labels,
        "diet": i.diet,
        "vegan": i.vegan or {},
        "flags": i.flags,
        "unavailable": i.unavailable,
        "is_special": i.is_special,
        "source": i.source,
        "from_saved": i.from_saved,
        "change_note": i.change_note,
        "version": i.version,
    }


def _menu(s: Session, dinner: Dinner, organiser: bool) -> dict:
    pages = s.exec(select(MenuPage).where(MenuPage.dinner_id == dinner.id).order_by(col(MenuPage.sort))).all()
    items = menu_items(s, dinner.id, include_unpublished=organiser)
    used_cats = {i.category_id for i in items}
    cats = s.exec(
        select(MenuCategory).where(MenuCategory.dinner_id == dinner.id).order_by(col(MenuCategory.sort))
    ).all()
    visible_pages = [p for p in pages if organiser or p.status == "published"]
    return {
        "pages": [
            {
                "id": p.id,
                "kind": p.kind,
                "status": p.status,
                "image": p.image_file,
                "legend": p.legend,
                "from_saved": p.from_saved,
                **({"error": p.error, "flags": p.flags} if organiser else {}),
            }
            for p in visible_pages
        ],
        "pending_pages": sum(1 for p in pages if p.status in ("processing", "review")),
        "legend": [e for p in visible_pages for e in (p.legend or [])],
        "categories": [
            {"id": c.id, "name": c.name, "note": c.note, "extras": c.extras}
            for c in cats
            if organiser or c.id in used_cats
        ],
        "items": [_item(i) for i in items],
    }


def _people(people: list[Participant], organiser: bool) -> list[dict]:
    out = []
    for p in people:
        row = {"id": p.id, "name": p.display_name, "is_organiser": p.is_organiser}
        if organiser:
            row["reference"] = p.reference
            row["has_session"] = bool(p.session_hash)
        out.append(row)
    return out


def guest_menu(s: Session, dinner: Dinner) -> dict:
    """The menu as guests see it: published pages plus dishes typed in by hand."""
    return _menu(s, dinner, organiser=False)


def guest_state(s: Session, dinner: Dinner, me: Participant | None, *, organiser: bool = False) -> dict:
    people = active_participants(s, dinner.id)
    lines = live_lines(s, dinner.id)
    allocs = line_allocations(s, [ln.id for ln in lines])
    by_line: dict[str, list] = {}
    for a in allocs:
        by_line.setdefault(a.line_id, []).append(a)
    bill = bill_for(s, dinner)
    state = {
        "dinner": _dinner(dinner),
        "me": None,
        "organiser": organiser,
        "participants": _people(people, organiser),
        "menu": _menu(s, dinner, organiser=False),
        "menu_share": menu_share.path_for(dinner.id),
        "lines": [serialize_line(ln, by_line.get(ln.id, [])) for ln in lines],
        "bill": bill.as_dict(),
        "shortlist": [],
        "payment": None,
    }
    receipt = active_receipt(s, dinner.id)
    state["receipt_image"] = receipt.image_files[0] if receipt and receipt.image_files else None
    if me is not None:
        state["me"] = {"id": me.id, "name": me.display_name, "reference": me.reference, "is_organiser": me.is_organiser}
        state["shortlist"] = list(s.exec(select(Shortlist.menu_item_id).where(Shortlist.participant_id == me.id)).all())
        state["payment"] = _my_payment(s, dinner, me)
    return state


def _my_payment(s: Session, dinner: Dinner, me: Participant) -> dict | None:
    if dinner.instructions_issued_at is None:
        return None
    shares = payments.shares_for(s, dinner)
    received = payments.received_by(s, dinner.id)
    status = payments.payment_status(me, dinner, shares.get(me.id, 0), received.get(me.id, 0))
    profile = payment_profile(s)
    status["instructions"] = (
        {
            "payid": profile.get("payid", ""),
            "payid_type": profile.get("payid_type", ""),
            "recipient_name": profile.get("recipient_name", ""),
            "bsb": profile.get("bsb", ""),
            "account_number": profile.get("account_number", ""),
            "reference": me.reference,
        }
        if dinner.status == "finalised" and not me.is_organiser
        else None
    )
    return status


def organiser_state(s: Session, dinner: Dinner) -> dict:
    me = s.get(Participant, dinner.organiser_participant_id) if dinner.organiser_participant_id else None
    state = guest_state(s, dinner, me, organiser=True)
    state["menu"] = _menu(s, dinner, organiser=True)
    state["restaurant"] = restaurants.summary(s, dinner)
    receipt = active_receipt(s, dinner.id)
    state["receipt"] = serialize_receipt(s, receipt) if receipt else None
    state["payment_statuses"] = payments.statuses(s, dinner)
    state["payments"] = [
        {
            "id": p.id,
            "participant_id": p.participant_id,
            "amount_cents": p.amount_cents,
            "source": p.source,
            "match_reason": p.match_reason,
            "bank_transaction_id": p.bank_transaction_id,
            "created_at": _iso(p.created_at),
            "undone_at": _iso(p.undone_at),
            "undo_reason": p.undo_reason,
        }
        for p in payments.payments_for(s, dinner.id)
    ]
    state["bank_review"] = [
        _tx(t)
        for t in payments.review_queue(s)
        if t.is_simulated == dinner.is_demo
        and (not t.candidates or any(c["dinner_id"] == dinner.id for c in t.candidates) or t.match_state == "unmatched")
    ]
    state["audit"] = [
        {"at": _iso(a.at), "actor": a.actor, "message": a.message}
        for a in s.exec(
            select(AuditEntry).where(AuditEntry.dinner_id == dinner.id).order_by(col(AuditEntry.id).desc()).limit(80)
        ).all()
    ]
    profile = payment_profile(s)
    state["profile_ready"] = bool(profile.get("payid") and profile.get("recipient_name"))
    return state


def _tx(t: BankTransaction) -> dict:
    return {
        "id": t.id,
        "amount_cents": t.amount_cents,
        "description": t.description,
        "message": t.message,
        "raw_text": t.raw_text,
        "created_at": _iso(t.created_at),
        "status": t.status,
        "match_state": t.match_state,
        "match_note": t.match_note,
        "candidates": t.candidates,
        "is_simulated": t.is_simulated,
    }
