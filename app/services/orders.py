"""Recording orders and sharing dishes.

Rules that matter:

* Prices for menu dishes come from the menu on the server, never from the
  phone. A guest can only type a price for something the menu doesn't price.
* Every edit quotes the line's `version`. If someone else got there first the
  edit is refused (409) and the phone refreshes, so nobody's share changes
  underneath them without their screen showing it.
* Joining a shared dish is its own explicit action. Recording a dish that is
  already on the table as shared prompts "join theirs, or record your own?"
  instead of quietly creating a duplicate.
* Every create carries a client-generated op id. A retry after a dropped
  connection returns the original result instead of recording it twice.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.models import (
    Allocation,
    ClientOp,
    Dinner,
    MenuCategory,
    MenuItem,
    OrderLine,
    Participant,
    utcnow,
)
from app.services.events import audit, touch

SPLIT_MODES = {"equal", "shares", "units", "proportional"}
LINE_KINDS = {"item", "surcharge", "discount", "adjustment"}
MAX_QTY = 99


class Actor:
    """Who is making a change: a guest (participant) and/or the organiser."""

    def __init__(self, participant: Participant | None, organiser: bool):
        self.participant = participant
        self.organiser = organiser

    @property
    def label(self) -> str:
        if self.organiser:
            return "organiser"
        return self.participant.display_name if self.participant else "unknown"

    @property
    def pid(self) -> str | None:
        return self.participant.id if self.participant else None


# ----------------------------------------------------------------- helpers
def ensure_open(dinner: Dinner) -> None:
    if dinner.status == "finalised":
        raise HTTPException(423, "The bill has been finalised. Ask the organiser to reopen it to make changes.")


def replay(s: Session, op_id: str | None, dinner: Dinner) -> dict | None:
    if not op_id:
        return None
    op = s.get(ClientOp, op_id)
    if op is None:
        return None
    if op.dinner_id != dinner.id:
        raise HTTPException(409, "Operation id reused.")
    return op.response


def remember(s: Session, op_id: str | None, dinner: Dinner, response: dict) -> dict:
    if op_id:
        s.add(ClientOp(op_id=op_id[:64], dinner_id=dinner.id, response=response))
    return response


def check_line_version(line: OrderLine, expected: int | None) -> None:
    if expected is None:
        raise HTTPException(422, "Missing version.")
    if line.version != expected:
        raise HTTPException(409, "This item changed a moment ago (someone else edited it). Showing the latest.")


def get_line(s: Session, dinner: Dinner, line_id: str) -> OrderLine:
    line = s.get(OrderLine, line_id)
    if line is None or line.dinner_id != dinner.id or line.deleted:
        raise HTTPException(404, "That item is no longer on the bill.")
    return line


def allocations_of(s: Session, line_id: str) -> list[Allocation]:
    return list(
        s.exec(select(Allocation).where(Allocation.line_id == line_id).order_by(col(Allocation.created_at))).all()
    )


def participant_in(s: Session, dinner: Dinner, pid: str) -> Participant:
    p = s.get(Participant, pid)
    if p is None or p.dinner_id != dinner.id or p.removed:
        raise HTTPException(422, "That person isn't at this dinner.")
    return p


def can_edit(line: OrderLine, actor: Actor, allocs: list[Allocation]) -> bool:
    if actor.organiser:
        return True
    if actor.pid is None:
        return False
    if line.created_by == actor.pid:
        return True
    # Anyone may edit a line that is theirs alone.
    return len(allocs) == 1 and allocs[0].participant_id == actor.pid


def bump(s: Session, line: OrderLine) -> None:
    line.version += 1
    line.updated_at = utcnow()
    s.add(line)


def serialize_line(line: OrderLine, allocs: list[Allocation]) -> dict:
    return {
        "id": line.id,
        "kind": line.kind,
        "source": line.source,
        "menu_item_id": line.menu_item_id,
        "name": line.name,
        "variant_label": line.variant_label,
        "extras": line.extras,
        "unit_price_cents": line.unit_price_cents,
        "quantity": line.quantity,
        "total_override_cents": line.total_override_cents,
        "note": line.note,
        "split_mode": line.split_mode,
        "shared": line.shared,
        "created_by": line.created_by,
        "not_on_receipt_ok": line.not_on_receipt_ok,
        "version": line.version,
        "created_at": line.created_at.isoformat(),
        "allocations": [{"participant_id": a.participant_id, "weight": a.weight} for a in allocs],
    }


def _qty(value) -> int:
    try:
        q = int(value)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, "Quantity must be a whole number.") from e
    if not 1 <= q <= MAX_QTY:
        raise HTTPException(422, f"Quantity must be between 1 and {MAX_QTY}.")
    return q


def _cents(value, what: str, *, allow_negative: bool = False) -> int:
    try:
        c = int(value)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, f"{what} must be a whole number of cents.") from e
    if c < 0 and not allow_negative:
        raise HTTPException(422, f"{what} can't be negative.")
    if abs(c) > 10_000_00:
        raise HTTPException(422, f"{what} looks too large.")
    return c


def _price_from_menu(
    s: Session, item: MenuItem, variant_label: str, extra_labels: list[str], manual_price
) -> tuple[int, str, list[dict]]:
    unit: int | None = item.price_cents
    chosen_variant = ""
    if item.variants:
        match = next((v for v in item.variants if v["label"] == variant_label), None)
        if match is None:
            raise HTTPException(422, f"Choose one of: {', '.join(v['label'] for v in item.variants)}.")
        chosen_variant = match["label"]
        unit = match.get("price_cents")
    elif variant_label:
        raise HTTPException(422, "This dish has no size options.")
    if unit is None:
        # The menu doesn't price it; accept what the guest was told, if given.
        unit = _cents(manual_price, "Price") if manual_price not in (None, "") else 0

    available = list(item.extras or [])
    if item.category_id:
        cat = s.get(MenuCategory, item.category_id)
        if cat and cat.extras:
            available += cat.extras
    extras = []
    for label in extra_labels:
        match = next((e for e in available if e["label"] == label), None)
        if match is None:
            raise HTTPException(422, f"'{label}' isn't an option for this dish.")
        extras.append({"label": match["label"], "price_cents": match.get("price_cents") or 0})
    return unit, chosen_variant, extras


# --------------------------------------------------------------- operations
def record_line(s: Session, dinner: Dinner, actor: Actor, data: dict) -> dict:
    """Record a dish, drink or manual item. Returns {"line": …} or {"exists": […]}."""
    ensure_open(dinner)
    if (prior := replay(s, data.get("op_id"), dinner)) is not None:
        return prior

    quantity = _qty(data.get("quantity", 1))
    note = str(data.get("note") or "").strip()[:300]
    shared = bool(data.get("shared"))
    kind = data.get("kind", "item")
    if kind not in LINE_KINDS:
        raise HTTPException(422, "Unknown line type.")
    if kind != "item" and not actor.organiser:
        raise HTTPException(403, "Only the organiser can add surcharges and adjustments.")

    menu_item_id = data.get("menu_item_id")
    if menu_item_id:
        item = s.get(MenuItem, menu_item_id)
        if item is None or item.dinner_id != dinner.id:
            raise HTTPException(404, "That dish isn't on this menu.")
        if item.unavailable and not actor.organiser:
            raise HTTPException(409, f"{item.name} is marked unavailable tonight.")
        unit, variant, extras = _price_from_menu(
            s,
            item,
            str(data.get("variant_label") or ""),
            list(data.get("extras") or []),
            data.get("unit_price_cents"),
        )
        name, source = item.name, "menu"

        if not data.get("separate"):
            existing = [
                ln
                for ln in s.exec(
                    select(OrderLine).where(
                        OrderLine.dinner_id == dinner.id,
                        OrderLine.menu_item_id == item.id,
                        OrderLine.deleted == False,  # noqa: E712
                    )
                ).all()
                if ln.shared and ln.variant_label == variant
            ]
            if existing:
                # Don't create a duplicate of a dish the table is already sharing.
                return {"exists": [serialize_line(ln, allocations_of(s, ln.id)) for ln in existing]}
    else:
        name = str(data.get("name") or "").strip()[:120]
        if not name:
            raise HTTPException(422, "Give the item a name.")
        unit = _cents(data.get("unit_price_cents", 0), "Price", allow_negative=kind != "item")
        variant, extras, source = "", [], data.get("source", "manual")
        if source not in ("manual", "receipt"):
            source = "manual"

    split_mode = data.get("split_mode") or ("proportional" if kind in ("surcharge", "discount") else "equal")
    if split_mode not in SPLIT_MODES:
        raise HTTPException(422, "Unknown split.")
    if split_mode == "proportional" and kind == "item":
        raise HTTPException(422, "Dishes are split by person, not proportionally.")

    line = OrderLine(
        dinner_id=dinner.id,
        kind=kind,
        source=source,
        menu_item_id=menu_item_id,
        name=name,
        variant_label=variant,
        extras=extras,
        unit_price_cents=unit,
        quantity=quantity,
        note=note,
        split_mode=split_mode,
        shared=shared,
        created_by=actor.pid,
    )
    if "total_cents" in data and data["total_cents"] is not None:
        if not actor.organiser:
            raise HTTPException(403, "Only the organiser can set a line total directly.")
        line.total_override_cents = _cents(data["total_cents"], "Total", allow_negative=True)
    s.add(line)
    s.flush()

    who = data.get("participants")
    if who is None:
        who = [actor.pid] if (actor.pid and kind == "item") else []
    weights = data.get("weights") or {}
    if who and not actor.organiser and not shared and who != [actor.pid]:
        raise HTTPException(403, "Mark it as shared to include other people.")
    _write_allocations(s, dinner, line, [(pid, int(weights.get(pid, 1))) for pid in who])

    audit(s, dinner.id, actor.label, f"Recorded {quantity} × {name}")
    touch(s, dinner)
    return remember(s, data.get("op_id"), dinner, {"line": serialize_line(line, allocations_of(s, line.id))})


def _write_allocations(s: Session, dinner: Dinner, line: OrderLine, pairs: list[tuple[str, int]]) -> None:
    seen = set()
    for pid, weight in pairs:
        if pid in seen:
            raise HTTPException(422, "Each person once, please.")
        seen.add(pid)
        participant_in(s, dinner, pid)
        if weight < 1 or weight > 999:
            raise HTTPException(422, "Shares must be between 1 and 999.")
    if line.split_mode == "units" and sum(w for _, w in pairs) > line.quantity:
        raise HTTPException(422, f"Only {line.quantity} to go around.")
    if line.split_mode == "equal":
        pairs = [(pid, 1) for pid, _ in pairs]
    for a in allocations_of(s, line.id):
        s.delete(a)
    s.flush()
    for pid, weight in pairs:
        s.add(Allocation(line_id=line.id, participant_id=pid, weight=weight))


def join_line(s: Session, dinner: Dinner, actor: Actor, line_id: str, version: int, units: int = 1) -> dict:
    ensure_open(dinner)
    me = actor.participant
    if me is None:
        raise HTTPException(401, "Join the dinner first.")
    line = get_line(s, dinner, line_id)
    check_line_version(line, version)
    allocs = allocations_of(s, line.id)
    mine = next((a for a in allocs if a.participant_id == me.id), None)
    if line.split_mode == "units":
        units = _qty(units)
        claimed = sum(a.weight for a in allocs if a.participant_id != me.id)
        if claimed + units > line.quantity:
            left = line.quantity - claimed
            raise HTTPException(409, f"Only {left} left to claim." if left else "All claimed already.")
        if mine:
            mine.weight = units
            s.add(mine)
        else:
            s.add(Allocation(line_id=line.id, participant_id=me.id, weight=units))
    else:
        if line.split_mode == "proportional":
            raise HTTPException(409, "This is split automatically.")
        if mine:
            raise HTTPException(409, "You're already sharing this.")
        s.add(Allocation(line_id=line.id, participant_id=me.id, weight=1))
    line.shared = True
    bump(s, line)
    audit(s, dinner.id, actor.label, f"Joined {line.name}")
    touch(s, dinner)
    return {"line": serialize_line(line, allocations_of(s, line.id))}


def leave_line(s: Session, dinner: Dinner, actor: Actor, line_id: str, version: int, pid: str | None = None) -> dict:
    ensure_open(dinner)
    target = pid or actor.pid
    if target is None:
        raise HTTPException(401, "Join the dinner first.")
    if target != actor.pid and not actor.organiser:
        raise HTTPException(403, "Only the organiser can remove someone else.")
    line = get_line(s, dinner, line_id)
    check_line_version(line, version)
    allocs = allocations_of(s, line.id)
    mine = next((a for a in allocs if a.participant_id == target), None)
    if mine is None:
        raise HTTPException(409, "Not on this item.")
    s.delete(mine)
    bump(s, line)
    audit(s, dinner.id, actor.label, f"Left {line.name}")
    touch(s, dinner)
    return {"line": serialize_line(line, allocations_of(s, line.id))}


EDITABLE = {
    "quantity",
    "note",
    "name",
    "unit_price_cents",
    "split_mode",
    "shared",
    "total_cents",
    "not_on_receipt_ok",
    "variant_label",
    "extras",
}


def update_line(s: Session, dinner: Dinner, actor: Actor, line_id: str, version: int, data: dict) -> dict:
    ensure_open(dinner)
    line = get_line(s, dinner, line_id)
    check_line_version(line, version)
    allocs = allocations_of(s, line.id)
    if not can_edit(line, actor, allocs):
        raise HTTPException(403, "Only the person who recorded this (or the organiser) can change it.")

    if "variant_label" in data or "extras" in data:
        if not line.menu_item_id:
            raise HTTPException(422, "Only menu dishes have options.")
        item = s.get(MenuItem, line.menu_item_id)
        manual = line.unit_price_cents if item and item.price_cents is None and not item.variants else None
        unit, variant, extras = _price_from_menu(
            s,
            item,
            str(data.get("variant_label", line.variant_label) or ""),
            list(data.get("extras", [e["label"] for e in line.extras])),
            manual,
        )
        line.unit_price_cents, line.variant_label, line.extras = unit, variant, extras
    if "quantity" in data:
        q = _qty(data["quantity"])
        if line.split_mode == "units" and sum(a.weight for a in allocs) > q:
            raise HTTPException(409, "More units are claimed than that — unclaim some first.")
        line.quantity = q
    if "note" in data:
        line.note = str(data["note"] or "").strip()[:300]
    if "name" in data:
        if line.menu_item_id and not actor.organiser:
            raise HTTPException(403, "Menu dishes keep their menu name.")
        name = str(data["name"] or "").strip()[:120]
        if not name:
            raise HTTPException(422, "Give the item a name.")
        line.name = name
    if "unit_price_cents" in data:
        menu_priced = False
        if line.menu_item_id:
            item = s.get(MenuItem, line.menu_item_id)
            menu_priced = bool(item and (item.price_cents is not None or item.variants))
        if menu_priced and not actor.organiser:
            raise HTTPException(403, "The menu sets this price. Ask the organiser to correct it.")
        line.unit_price_cents = _cents(data["unit_price_cents"], "Price", allow_negative=line.kind != "item")
    if "total_cents" in data:
        if not actor.organiser:
            raise HTTPException(403, "Only the organiser can override a total.")
        line.total_override_cents = (
            None if data["total_cents"] is None else _cents(data["total_cents"], "Total", allow_negative=True)
        )
    if "split_mode" in data:
        mode = data["split_mode"]
        if mode not in SPLIT_MODES or (mode == "proportional" and line.kind == "item"):
            raise HTTPException(422, "That split doesn't apply here.")
        if mode != line.split_mode:
            if not actor.organiser and len(allocs) > 1:
                raise HTTPException(403, "Only the organiser can change how a shared item is split.")
            line.split_mode = mode
            if mode == "equal":
                for a in allocs:
                    a.weight = 1
                    s.add(a)
            elif mode == "units":
                if sum(a.weight for a in allocs) > line.quantity:
                    for a in allocs:
                        a.weight = 1
                        s.add(a)
                    if len(allocs) > line.quantity:
                        raise HTTPException(409, "More people than units — adjust first.")
    if "shared" in data:
        line.shared = bool(data["shared"])
    if "not_on_receipt_ok" in data:
        if not actor.organiser:
            raise HTTPException(403, "Organiser only.")
        line.not_on_receipt_ok = bool(data["not_on_receipt_ok"])
    bump(s, line)
    audit(s, dinner.id, actor.label, f"Edited {line.name}")
    touch(s, dinner)
    return {"line": serialize_line(line, allocations_of(s, line.id))}


def set_allocations(
    s: Session,
    dinner: Dinner,
    actor: Actor,
    line_id: str,
    version: int,
    allocations: list[dict],
    split_mode: str | None = None,
) -> dict:
    ensure_open(dinner)
    line = get_line(s, dinner, line_id)
    check_line_version(line, version)
    if not actor.organiser and line.created_by != actor.pid:
        raise HTTPException(403, "Only the organiser can reallocate someone else's item.")
    if split_mode:
        if split_mode not in SPLIT_MODES or (split_mode == "proportional" and line.kind == "item"):
            raise HTTPException(422, "That split doesn't apply here.")
        line.split_mode = split_mode
    pairs = [(str(a["participant_id"]), int(a.get("weight", 1))) for a in allocations]
    if not actor.organiser and len(pairs) > 1:
        line.shared = True
    _write_allocations(s, dinner, line, pairs)
    bump(s, line)
    audit(s, dinner.id, actor.label, f"Reallocated {line.name}")
    touch(s, dinner)
    return {"line": serialize_line(line, allocations_of(s, line.id))}


def delete_line(s: Session, dinner: Dinner, actor: Actor, line_id: str, version: int) -> dict:
    ensure_open(dinner)
    line = get_line(s, dinner, line_id)
    check_line_version(line, version)
    allocs = allocations_of(s, line.id)
    if not can_edit(line, actor, allocs):
        raise HTTPException(403, "Only the person who recorded this (or the organiser) can remove it.")
    if not actor.organiser and any(a.participant_id != actor.pid for a in allocs):
        raise HTTPException(409, "Others are sharing this. Leave it instead, or ask the organiser.")
    line.deleted = True
    bump(s, line)
    audit(s, dinner.id, actor.label, f"Removed {line.name}")
    touch(s, dinner)
    return {"deleted": line.id}
