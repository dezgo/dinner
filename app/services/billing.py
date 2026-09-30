"""Who owes what.

Pure calculation over order lines and allocations: nothing here writes. The
same function drives every screen (guest totals, the organiser's bill, the
finalisation gate), so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.models import Allocation, Dinner, OrderLine, Participant, Receipt, ReceiptLine
from app.services.money import fmt, split_cents

UNALLOCATED = "__unallocated__"


def line_amount(line: OrderLine) -> int:
    if line.total_override_cents is not None:
        return line.total_override_cents
    per_unit = line.unit_price_cents + sum(int(e.get("price_cents") or 0) for e in line.extras)
    return per_unit * line.quantity


def line_needs_price(line: OrderLine) -> bool:
    return (
        line.kind == "item"
        and line.total_override_cents is None
        and line.unit_price_cents == 0
        and not any(e.get("price_cents") for e in line.extras)
    )


@dataclass
class LineResult:
    amount: int
    shares: dict[str, int] = field(default_factory=dict)
    unallocated: int = 0


@dataclass
class Bill:
    lines: dict[str, LineResult]
    people: dict[str, dict[str, int]]
    recorded_total: int
    allocated_total: int
    unallocated_total: int
    bill_total: int | None
    bill_total_source: str | None
    issues: list[dict]

    @property
    def difference(self) -> int | None:
        if self.bill_total is None:
            return None
        return self.bill_total - self.recorded_total

    @property
    def accounted_for(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "lines": {
                k: {"amount": v.amount, "shares": v.shares, "unallocated": v.unallocated} for k, v in self.lines.items()
            },
            "people": self.people,
            "recorded_total": self.recorded_total,
            "allocated_total": self.allocated_total,
            "unallocated_total": self.unallocated_total,
            "bill_total": self.bill_total,
            "bill_total_source": self.bill_total_source,
            "difference": self.difference,
            "issues": self.issues,
            "accounted_for": self.accounted_for,
        }


def compute_bill(
    participants: Sequence[Participant],
    lines: Iterable[OrderLine],
    allocations: Iterable[Allocation],
    *,
    bill_total: int | None = None,
    bill_total_source: str | None = None,
    extra_issues: Sequence[dict] = (),
) -> Bill:
    order = [p.id for p in participants]
    position = {pid: i for i, pid in enumerate(order)}
    live = [ln for ln in lines if not ln.deleted]
    by_line: dict[str, list[Allocation]] = {}
    for a in allocations:
        if a.participant_id in position:
            by_line.setdefault(a.line_id, []).append(a)

    people = {pid: {"items": 0, "adjustments": 0, "total": 0} for pid in order}
    results: dict[str, LineResult] = {}
    issues: list[dict] = []

    def ordered_weights(allocs: list[Allocation]) -> list[tuple[str, int]]:
        allocs = sorted(allocs, key=lambda a: position[a.participant_id])
        return [(a.participant_id, a.weight) for a in allocs if a.weight > 0]

    # Items and directly-allocated adjustments first; proportional ones need
    # everyone's item subtotal.
    direct = [ln for ln in live if ln.split_mode != "proportional"]
    proportional = [ln for ln in live if ln.split_mode == "proportional"]

    for ln in direct:
        amount = line_amount(ln)
        res = LineResult(amount=amount)
        weights = ordered_weights(by_line.get(ln.id, []))
        if ln.split_mode == "units":
            claimed = sum(w for _, w in weights)
            spare = max(ln.quantity - claimed, 0)
            if weights or spare:
                split = split_cents(amount, weights + ([(UNALLOCATED, spare)] if spare else []))
                res.unallocated = split.pop(UNALLOCATED, 0)
                res.shares = split
            else:
                res.unallocated = amount
        elif weights:
            res.shares = split_cents(amount, weights)
        else:
            res.unallocated = amount
        results[ln.id] = res
        bucket = "items" if ln.kind == "item" else "adjustments"
        for pid, cents in res.shares.items():
            people[pid][bucket] += cents

    for ln in proportional:
        amount = line_amount(ln)
        res = LineResult(amount=amount)
        chosen = {a.participant_id for a in by_line.get(ln.id, [])}
        # Proportional over everyone with items, or over a chosen subset.
        base = [
            (pid, people[pid]["items"]) for pid in order if people[pid]["items"] > 0 and (not chosen or pid in chosen)
        ]
        if base:
            res.shares = split_cents(amount, base)
        else:
            res.unallocated = amount
        results[ln.id] = res
        for pid, cents in res.shares.items():
            people[pid]["adjustments"] += cents

    for p in people.values():
        p["total"] = p["items"] + p["adjustments"]

    recorded = sum(r.amount for r in results.values())
    unallocated = sum(r.unallocated for r in results.values())

    names = {ln.id: ln.name for ln in live}
    for ln in live:
        if line_needs_price(ln):
            issues.append({"code": "no_price", "line_id": ln.id, "message": f"{ln.name} has no price yet."})
    for lid, r in results.items():
        if r.unallocated:
            issues.append(
                {
                    "code": "unallocated",
                    "line_id": lid,
                    "message": f"{names[lid]}: {fmt(r.unallocated)} not yet allocated to anyone.",
                }
            )
    if bill_total is None:
        issues.append(
            {
                "code": "no_total",
                "message": "Enter the restaurant's total, or scan the itemised receipt.",
            }
        )
    elif bill_total != recorded:
        diff = bill_total - recorded
        direction = "more" if diff > 0 else "less"
        issues.append(
            {
                "code": "difference",
                "message": (
                    f"The restaurant's total is {fmt(abs(diff))} {direction} than the recorded "
                    "items. Correct the items, or add a labelled adjustment."
                ),
            }
        )
    issues.extend(extra_issues)

    return Bill(
        lines=results,
        people=people,
        recorded_total=recorded,
        allocated_total=recorded - unallocated,
        unallocated_total=unallocated,
        bill_total=bill_total,
        bill_total_source=bill_total_source,
        issues=issues,
    )


def active_participants(session: Session, dinner_id: str) -> list[Participant]:
    return list(
        session.exec(
            select(Participant)
            .where(Participant.dinner_id == dinner_id, Participant.removed == False)  # noqa: E712
            .order_by(col(Participant.sort), col(Participant.created_at))
        ).all()
    )


def live_lines(session: Session, dinner_id: str) -> list[OrderLine]:
    return list(
        session.exec(
            select(OrderLine)
            .where(OrderLine.dinner_id == dinner_id, OrderLine.deleted == False)  # noqa: E712
            .order_by(col(OrderLine.created_at))
        ).all()
    )


def line_allocations(session: Session, line_ids: list[str]) -> list[Allocation]:
    if not line_ids:
        return []
    return list(
        session.exec(
            select(Allocation).where(col(Allocation.line_id).in_(line_ids)).order_by(col(Allocation.created_at))
        ).all()
    )


def receipt_issues(session: Session, dinner: Dinner, lines: list[OrderLine]) -> list[dict]:
    """Outstanding reconciliation work that must be finished before finalising."""
    receipt = active_receipt(session, dinner.id)
    if receipt is None:
        return []
    if receipt.status == "processing":
        return [{"code": "receipt_processing", "message": "The receipt is still being read."}]
    if receipt.status != "review":
        return []
    issues = []
    rlines = session.exec(select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id)).all()
    pending = [r for r in rlines if r.state == "pending"]
    if pending:
        issues.append(
            {
                "code": "receipt_pending",
                "message": f"{len(pending)} receipt line(s) still need matching or adding.",
            }
        )
    matched = {lid for r in rlines for lid in (r.matched_line_ids or [])}
    matched |= {r.added_line_id for r in rlines if r.added_line_id}
    # Lines already suggested for a pending receipt line are covered above.
    matched |= {lid for r in pending for lid in (r.suggestion or {}).get("line_ids", [])}
    loose = [ln for ln in lines if ln.kind == "item" and ln.id not in matched and not ln.not_on_receipt_ok]
    for ln in loose:
        issues.append(
            {
                "code": "not_on_receipt",
                "line_id": ln.id,
                "message": f"{ln.name} was recorded but isn't matched to the receipt.",
            }
        )
    return issues


def active_receipt(session: Session, dinner_id: str) -> Receipt | None:
    return session.exec(
        select(Receipt)
        .where(Receipt.dinner_id == dinner_id, Receipt.status != "discarded")
        .order_by(col(Receipt.created_at).desc())
    ).first()


def bill_for(session: Session, dinner: Dinner) -> Bill:
    people = active_participants(session, dinner.id)
    lines = live_lines(session, dinner.id)
    allocs = line_allocations(session, [ln.id for ln in lines])
    return compute_bill(
        people,
        lines,
        allocs,
        bill_total=dinner.bill_total_cents,
        bill_total_source=dinner.bill_total_source,
        extra_issues=receipt_issues(session, dinner, lines),
    )
