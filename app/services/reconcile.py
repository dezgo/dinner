"""Receipts: reading them and reconciling them with recorded orders.

The bill is always the set of order lines. A receipt is a check against it:

* A receipt line that matches recorded lines is *matched* — the recorded lines
  (and everyone's shares of them) stay as they are. At most, their quantity or
  price is corrected to the receipt's figure. Nothing is added, so nothing is
  double-counted.
* A receipt line with no recorded counterpart can be *added* as a new line,
  which people then claim. In receipt-first dinners that is every line.
* Informational lines (GST included, subtotal, total, card payment) are
  ignored automatically; GST is never added on top of GST-inclusive prices.
* Recorded lines the receipt doesn't show are listed as missing until the
  organiser removes them or confirms they should stay.

Only exact matches (same name, quantity and price) are confirmed without a
person looking; everything else is a suggestion awaiting review.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.db import locked_write, session_scope
from app.models import Dinner, OrderLine, Receipt, ReceiptLine
from app.services import jobs
from app.services.billing import line_amount, live_lines
from app.services.events import audit, touch
from app.services.extraction import ExtractionError, ReceiptExtraction, get_extractor, receipt_flags
from app.services.images import read_image
from app.services.money import fmt, split_cents
from app.services.orders import Actor, allocations_of, ensure_open, record_line

logger = logging.getLogger(__name__)

AUTO_IGNORE = {"tax_info", "subtotal", "total", "payment"}
KIND_TO_LINE = {
    "item": "item",
    "surcharge": "surcharge",
    "discount": "discount",
    "tip": "adjustment",
    "other": "adjustment",
}


def queue_receipt(receipt_id: str) -> None:
    jobs.submit(process_receipt, receipt_id)


def process_receipt(receipt_id: str) -> None:
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if r is None or r.status != "processing":
            return
        dinner = s.get(Dinner, r.dinner_id)
        images = [(read_image(r.dinner_id, f), "image/jpeg") for f in r.image_files]
        demo = bool(dinner and dinner.is_demo)
    try:
        result = get_extractor(demo=demo).receipt(images)
    except ExtractionError as e:
        _fail(receipt_id, str(e))
        return
    except Exception:
        logger.exception("receipt extraction crashed")
        _fail(receipt_id, "Something went wrong reading the receipt. Retry, or enter the total.")
        return
    apply_receipt(receipt_id, result)


def _fail(receipt_id: str, message: str) -> None:
    with locked_write() as s:
        r = s.get(Receipt, receipt_id)
        if r is None:
            return
        r.status, r.error = "failed", message
        s.add(r)
        touch(s, s.get(Dinner, r.dinner_id))


def apply_receipt(receipt_id: str, result: ReceiptExtraction) -> None:
    with locked_write() as s:
        r = s.get(Receipt, receipt_id)
        if r is None or r.status != "processing":
            return
        dinner = s.get(Dinner, r.dinner_id)
        r.total_cents = result.total_cents
        r.subtotal_cents = result.subtotal_cents
        r.gst_cents = result.gst_cents
        r.gst_included = result.gst_included
        r.flags = receipt_flags(result) + list(result.notes)
        for i, x in enumerate(result.lines):
            flags = [x.note] if x.uncertain and x.note else (["Unclear on the receipt"] if x.uncertain else [])
            state = "ignored" if x.kind in AUTO_IGNORE else "pending"
            s.add(
                ReceiptLine(
                    receipt_id=r.id,
                    dinner_id=r.dinner_id,
                    sort=i,
                    description=x.description.strip() or "(unreadable)",
                    quantity=max(1, x.quantity),
                    unit_price_cents=x.unit_price_cents,
                    line_total_cents=x.line_total_cents,
                    kind=x.kind,
                    flags=flags,
                    state=state,
                )
            )
        r.status = "review"
        s.add(r)
        s.flush()
        if result.total_cents is not None and dinner.status == "open":
            dinner.bill_total_cents = result.total_cents
            dinner.bill_total_source = "receipt"
            dinner.version += 1
        suggest_matches(s, dinner, r)
        audit(s, dinner.id, "system", f"Read receipt ({len(result.lines)} lines, total {fmt(result.total_cents)})")
        touch(s, dinner, "receipt")


# ---------------------------------------------------------------- matching
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    words = _WORD.findall(text.lower())
    # Drop quantity markers like "2", "x", "2x", "x2" that receipts prefix.
    return [w for w in words if not re.fullmatch(r"\d+x?|x\d*", w)] or words


def _is_abbrev(token: str, word: str) -> bool:
    """'grlc' abbreviates 'garlic'; 'brd' abbreviates 'bread'."""
    if not token or not word or token[0] != word[0] or len(token) > len(word):
        return False
    it = iter(word)
    return all(ch in it for ch in token)


def name_score(receipt_text: str, recorded: str) -> float:
    a, b = _words(receipt_text), _words(recorded)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, "".join(a), "".join(b)).ratio()
    hits = sum(1 for t in a if any(t == w or _is_abbrev(t, w) for w in b))
    coverage = hits / len(a)
    return max(ratio, 0.85 * coverage if coverage == 1 else 0.6 * coverage)


def near_identical(receipt_text: str, recorded: str) -> bool:
    a, b = "".join(_words(receipt_text)), "".join(_words(recorded))
    return bool(a) and SequenceMatcher(None, a, b).ratio() >= 0.9


def _unit(line: OrderLine) -> int:
    return line_amount(line) // max(line.quantity, 1)


def suggest_matches(s: Session, dinner: Dinner, receipt: Receipt) -> None:
    rlines = s.exec(
        select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id).order_by(col(ReceiptLine.sort))
    ).all()
    lines = [ln for ln in live_lines(s, dinner.id) if ln.kind == "item"]
    taken = {lid for r in rlines for lid in (r.matched_line_ids or [])}
    taken |= {r.added_line_id for r in rlines if r.added_line_id}

    for r in rlines:
        if r.state != "pending" or r.kind != "item":
            continue
        free = [ln for ln in lines if ln.id not in taken]
        scored = sorted(
            ((name_score(r.description, _label(ln)), ln) for ln in free),
            key=lambda t: -t[0],
        )
        scored = [(sc, ln) for sc, ln in scored if sc >= 0.55]
        if not scored:
            r.suggestion = {}
            s.add(r)
            continue
        best = scored[0][0]
        group = [ln for sc, ln in scored if sc >= best - 0.05 and _label(ln) == _label(scored[0][1])]
        chosen, qty = [], 0
        for ln in group:
            if qty >= r.quantity:
                break
            chosen.append(ln)
            qty += ln.quantity
        recorded_total = sum(line_amount(ln) for ln in chosen)
        reasons = []
        if not near_identical(r.description, _label(chosen[0])):
            reasons.append("Name differs (likely abbreviated) — check it's the same dish")
        if qty != r.quantity:
            reasons.append(f"Quantity differs: receipt {r.quantity}, recorded {qty}")
        if r.line_total_cents is not None and recorded_total != r.line_total_cents:
            reasons.append(f"Price differs: receipt {fmt(r.line_total_cents)}, recorded {fmt(recorded_total)}")
        if r.line_total_cents is None:
            reasons.append("The receipt amount couldn't be read")
        if len([1 for sc, _ in scored if sc >= best - 0.05]) > len(group):
            reasons.append("Several recorded dishes look similar")
        r.suggestion = {
            "line_ids": [ln.id for ln in chosen],
            "score": round(best, 2),
            "reasons": reasons,
            "recorded_total": recorded_total,
            "recorded_quantity": qty,
        }
        if not reasons and not r.flags:
            r.state = "matched"
            r.matched_line_ids = [ln.id for ln in chosen]
            r.suggestion["auto"] = True
            taken |= set(r.matched_line_ids)
        s.add(r)


def _label(line: OrderLine) -> str:
    return f"{line.name} {line.variant_label}".strip()


# ----------------------------------------------------------------- actions
def _receipt_line(s: Session, dinner: Dinner, rline_id: str) -> ReceiptLine:
    r = s.get(ReceiptLine, rline_id)
    if r is None or r.dinner_id != dinner.id:
        raise HTTPException(404, "Receipt line not found.")
    return r


def confirm_match(s: Session, dinner: Dinner, rline_id: str, line_ids: list[str], use_receipt: bool) -> None:
    ensure_open(dinner)
    r = _receipt_line(s, dinner, rline_id)
    if r.state not in ("pending", "matched"):
        raise HTTPException(409, "Undo this line's current resolution first.")
    others = s.exec(select(ReceiptLine).where(ReceiptLine.receipt_id == r.receipt_id, ReceiptLine.id != r.id)).all()
    used = {lid for o in others for lid in (o.matched_line_ids or [])} | {o.added_line_id for o in others}
    lines = []
    for lid in line_ids:
        ln = s.get(OrderLine, lid)
        if ln is None or ln.dinner_id != dinner.id or ln.deleted:
            raise HTTPException(404, "A recorded item has gone.")
        if lid in used:
            raise HTTPException(409, f"{ln.name} is already matched to another receipt line.")
        lines.append(ln)
    if not lines:
        raise HTTPException(422, "Pick at least one recorded item.")

    if use_receipt and r.line_total_cents is not None:
        if len(lines) == 1:
            ln = lines[0]
            claimed = sum(a.weight for a in allocations_of(s, ln.id)) if ln.split_mode == "units" else 0
            if claimed > r.quantity:
                raise HTTPException(409, "More units are claimed than the receipt shows — fix claims first.")
            ln.quantity = r.quantity
        amounts = [(ln.id, max(line_amount(ln), 0) or ln.quantity) for ln in lines]
        split = split_cents(r.line_total_cents, amounts)
        for ln in lines:
            if line_amount(ln) != split[ln.id]:
                ln.total_override_cents = split[ln.id]
            ln.version += 1
            s.add(ln)
    r.state = "matched"
    r.matched_line_ids = [ln.id for ln in lines]
    s.add(r)
    audit(s, dinner.id, "organiser", f"Matched receipt '{r.description}'")
    touch(s, dinner, "receipt")


def add_as_new(s: Session, dinner: Dinner, rline_id: str, actor: Actor) -> None:
    ensure_open(dinner)
    r = _receipt_line(s, dinner, rline_id)
    if r.state != "pending":
        raise HTTPException(409, "This receipt line is already resolved.")
    if r.line_total_cents is None:
        raise HTTPException(422, "Enter the amount for this line first.")
    kind = KIND_TO_LINE.get(r.kind, "adjustment")
    result = record_line(
        s,
        dinner,
        actor,
        {
            "name": r.description,
            "kind": kind,
            "source": "receipt",
            "quantity": r.quantity,
            "unit_price_cents": r.line_total_cents // r.quantity,
            "total_cents": r.line_total_cents,
            "split_mode": "units" if kind == "item" and r.quantity > 1 else None,
            "participants": [],
        },
    )
    r.state = "added"
    r.added_line_id = result["line"]["id"]
    s.add(r)


def ignore(s: Session, dinner: Dinner, rline_id: str) -> None:
    ensure_open(dinner)
    r = _receipt_line(s, dinner, rline_id)
    if r.state != "pending":
        raise HTTPException(409, "This receipt line is already resolved.")
    r.state = "ignored"
    s.add(r)
    touch(s, dinner, "receipt")


def reset(s: Session, dinner: Dinner, rline_id: str) -> None:
    """Undo a match, an addition or an ignore."""
    ensure_open(dinner)
    r = _receipt_line(s, dinner, rline_id)
    if r.state == "added" and r.added_line_id:
        ln = s.get(OrderLine, r.added_line_id)
        if ln and not ln.deleted:
            if allocations_of(s, ln.id):
                raise HTTPException(409, "People have claimed this item. Remove their claims first.")
            ln.deleted = True
            ln.version += 1
            s.add(ln)
        r.added_line_id = None
    r.state = "pending"
    r.matched_line_ids = []
    s.add(r)
    touch(s, dinner, "receipt")


def edit_receipt_line(s: Session, dinner: Dinner, rline_id: str, data: dict) -> None:
    ensure_open(dinner)
    r = _receipt_line(s, dinner, rline_id)
    if r.state != "pending":
        raise HTTPException(409, "Undo this line's resolution before editing it.")
    if "description" in data:
        r.description = str(data["description"]).strip()[:200] or r.description
    if "quantity" in data:
        r.quantity = max(1, int(data["quantity"]))
    if "line_total_cents" in data:
        r.line_total_cents = None if data["line_total_cents"] is None else int(data["line_total_cents"])
    if "kind" in data and data["kind"] in KIND_TO_LINE.keys() | AUTO_IGNORE:
        r.kind = data["kind"]
    r.flags = []
    s.add(r)
    touch(s, dinner, "receipt")


def resuggest(s: Session, dinner: Dinner, receipt: Receipt) -> None:
    suggest_matches(s, dinner, receipt)
    touch(s, dinner, "receipt")


def serialize_receipt(s: Session, receipt: Receipt) -> dict:
    rlines = s.exec(
        select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id).order_by(col(ReceiptLine.sort))
    ).all()
    return {
        "id": receipt.id,
        "status": receipt.status,
        "error": receipt.error,
        "images": receipt.image_files,
        "total_cents": receipt.total_cents,
        "subtotal_cents": receipt.subtotal_cents,
        "gst_cents": receipt.gst_cents,
        "gst_included": receipt.gst_included,
        "flags": receipt.flags,
        "lines": [
            {
                "id": r.id,
                "description": r.description,
                "quantity": r.quantity,
                "unit_price_cents": r.unit_price_cents,
                "line_total_cents": r.line_total_cents,
                "kind": r.kind,
                "flags": r.flags,
                "state": r.state,
                "matched_line_ids": r.matched_line_ids,
                "added_line_id": r.added_line_id,
                "suggestion": r.suggestion,
            }
            for r in rlines
        ],
    }
