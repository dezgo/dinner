"""Payment status, matching incoming transfers, and manual confirmation.

Matching works on facts Up documents for a transaction: the account it landed
in, that it is a credit, when it was created, its exact amount, and three
free-text fields (`message`, `description`, `rawText`). Up does not document
which field carries a PayID/Osko payer's reference or name, so the reference
is searched for in all three, and a name in the text only ever counts as
supporting evidence.

Automatic confirmation happens only when:
  1. exactly one outstanding person's reference is in the text, standing on
     its own, and the amount is exactly what they owe; or
  2. no reference is present, exactly one person anywhere owes exactly this
     amount, only one dinner's payment window is open, and nothing in the
     text points elsewhere.
Everything else with a plausible candidate goes to the organiser's review
queue with the reasons shown. A transfer can back at most one payment (also
enforced by a unique index), and every decision is recorded with its reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.models import BankTransaction, Dinner, Participant, Payment, utcnow
from app.services.billing import active_participants, bill_for
from app.services.events import audit, touch
from app.services.money import fmt
from app.services.references import compact, reference_found

CLOCK_GRACE = timedelta(minutes=10)
_CODE_IN_TEXT = re.compile(r"(?<![A-Z0-9])DIN[\s\-_]*(\d{1,5})(?!\d)")


# ------------------------------------------------------------------ status
def received_by(s: Session, dinner_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in s.exec(
        select(Payment).where(Payment.dinner_id == dinner_id, Payment.undone_at == None)  # noqa: E711
    ).all():
        out[p.participant_id] = out.get(p.participant_id, 0) + p.amount_cents
    return out


def shares_for(s: Session, dinner: Dinner) -> dict[str, int]:
    """What each person owes: locked shares once finalised, live otherwise."""
    if dinner.status == "finalised" and dinner.final_shares:
        return dict(dinner.final_shares)
    return {pid: p["total"] for pid, p in bill_for(s, dinner).people.items()}


def payment_status(participant: Participant, dinner: Dinner, share: int, received: int) -> dict:
    balance = share - received
    if participant.is_organiser:
        state = "self"
    elif dinner.instructions_issued_at is None:
        state = "not_ready"
    elif received and balance == 0:
        state = "confirmed"
    elif balance < 0:
        state = "overpaid"
    elif received and balance > 0:
        state = "part_paid"
    elif share == 0:
        state = "nothing_owed"
    elif participant.marked_sent_at:
        state = "marked_sent"
    else:
        state = "awaiting"
    return {
        "state": state,
        "share": share,
        "received": received,
        "balance": balance,
        "marked_sent_at": participant.marked_sent_at.isoformat() if participant.marked_sent_at else None,
    }


def statuses(s: Session, dinner: Dinner) -> dict[str, dict]:
    shares = shares_for(s, dinner)
    received = received_by(s, dinner.id)
    return {
        p.id: payment_status(p, dinner, shares.get(p.id, 0), received.get(p.id, 0))
        for p in active_participants(s, dinner.id)
    }


# ---------------------------------------------------------------- matching
@dataclass
class Candidate:
    participant: Participant
    dinner: Dinner
    outstanding: int
    ref: str | None = None
    amount_exact: bool = False
    name_hint: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "participant_id": self.participant.id,
            "dinner_id": self.dinner.id,
            "name": self.participant.display_name,
            "dinner": self.dinner.restaurant_name or self.dinner.code,
            "reference": self.participant.reference,
            "outstanding": self.outstanding,
            "reasons": self.reasons,
        }


def open_windows(s: Session, at: datetime, simulated: bool) -> list[Dinner]:
    dinners = s.exec(
        select(Dinner).where(
            Dinner.instructions_issued_at != None,  # noqa: E711
            Dinner.archived == False,  # noqa: E712
            Dinner.is_demo == simulated,
        )
    ).all()
    return [
        d
        for d in dinners
        if d.instructions_issued_at - CLOCK_GRACE <= at
        and (d.payment_window_ends_at is None or at <= d.payment_window_ends_at)
    ]


def _name_hint(p: Participant, tx: BankTransaction) -> bool:
    words = [w for w in re.findall(r"[A-Za-z]{3,}", p.display_name)]
    text = compact(" ".join([tx.description, tx.raw_text]))
    return bool(words) and all(compact(w) in text for w in words)


def evaluate(s: Session, tx: BankTransaction) -> tuple[str, Candidate | None, list[Candidate], str]:
    """Decide what to do with a credit: ('auto'|'review'|'unmatched', …)."""
    texts = (tx.message, tx.description, tx.raw_text)
    dinners = open_windows(s, tx.created_at, tx.is_simulated)
    cands: list[Candidate] = []
    conflicts: list[str] = []
    for d in dinners:
        shares = shares_for(s, d)
        received = received_by(s, d.id)
        for p in active_participants(s, d.id):
            if p.is_organiser:
                continue
            outstanding = shares.get(p.id, 0) - received.get(p.id, 0)
            ref = reference_found(p.reference, *texts)
            if outstanding <= 0:
                if ref:
                    conflicts.append(
                        f"Reference {p.reference} is in the transfer but {p.display_name} has already paid."
                    )
                continue
            c = Candidate(
                p, d, outstanding, ref=ref, amount_exact=tx.amount_cents == outstanding, name_hint=_name_hint(p, tx)
            )
            if ref == "exact":
                c.reasons.append(f"Reference {p.reference} in the transfer")
            elif ref == "loose":
                c.reasons.append(f"Text contains {p.reference}, but run together with other characters")
            if c.amount_exact:
                c.reasons.append(f"Exact amount owed ({fmt(outstanding)})")
            else:
                c.reasons.append(f"Owes {fmt(outstanding)}, transfer is {fmt(tx.amount_cents)}")
            if c.name_hint:
                c.reasons.append("Sender text contains their name (supporting only)")
            cands.append(c)

    open_codes = {d.code for d in dinners}
    for m in _CODE_IN_TEXT.finditer(" ".join(t or "" for t in texts).upper()):
        code = f"DIN{m.group(1)}"
        if code not in open_codes:
            conflicts.append(f"The text mentions {code}, which has no open payment window.")

    # "DIN7-HELEN2" standing on its own also contains "DIN7HELEN" run
    # together. A longer reference found exactly explains a shorter one found
    # only loosely, so that partial hit is not treated as competing evidence.
    exact_refs_all = [compact(c.participant.reference) for c in cands if c.ref == "exact"]
    for c in cands:
        mine = compact(c.participant.reference)
        if c.ref == "loose" and any(e != mine and e.startswith(mine) for e in exact_refs_all):
            c.ref = None
            c.reasons = [r for r in c.reasons if "run together" not in r]

    ref_hits = [c for c in cands if c.ref]
    if ref_hits:
        exact_refs = [c for c in ref_hits if c.ref == "exact"]
        if len(ref_hits) == 1 and exact_refs and not conflicts:
            c = exact_refs[0]
            if c.amount_exact:
                return "auto", c, cands, f"Reference {c.participant.reference} and exact amount"
            return "review", c, ref_hits, "Reference matches but the amount differs from what's owed."
        amount_ok = [c for c in ref_hits if c.amount_exact]
        if len(amount_ok) == 1 and amount_ok[0].ref == "exact" and not conflicts:
            c = amount_ok[0]
            return (
                "auto",
                c,
                cands,
                (f"Reference {c.participant.reference} and exact amount (a similar reference was ruled out by amount)"),
            )
        return "review", None, ref_hits, "More than one reference could apply."

    by_amount = [c for c in cands if c.amount_exact]
    if len(by_amount) == 1:
        c = by_amount[0]
        others_named = [o for o in cands if o.name_hint and o is not c]
        if len(dinners) == 1 and not conflicts and not others_named:
            return (
                "auto",
                c,
                cands,
                (
                    f"Only outstanding share of exactly {fmt(tx.amount_cents)} while this dinner's "
                    "payment window is open (no reference found)"
                ),
            )
        why = "Several dinners are collecting payments." if len(dinners) > 1 else "Conflicting details in the transfer."
        return "review", c, by_amount + others_named, f"Amount matches {c.participant.display_name}, but: {why}"
    if len(by_amount) > 1:
        return "review", None, by_amount, f"{len(by_amount)} people owe exactly {fmt(tx.amount_cents)}."
    if conflicts:
        return "review", None, [], " ".join(conflicts)
    return "unmatched", None, [], "No outstanding share matches this transfer."


def ingest(s: Session, data: dict, *, simulated: bool = False) -> BankTransaction | None:
    """Record and match one credit. Safe to call any number of times per transaction."""
    if data["amount_cents"] <= 0:
        return None
    tx = s.get(BankTransaction, data["id"])
    if tx is not None:
        tx.status = data.get("status", tx.status)
        s.add(tx)
        if tx.status == "DELETED":
            _reverse(s, tx)
        return tx
    if data.get("status") == "DELETED":
        return None
    if not open_windows(s, data["created_at"], simulated):
        # Nothing is being collected; this credit is none of our business.
        return None
    tx = BankTransaction(
        id=data["id"],
        account_id=data.get("account_id", ""),
        status=data.get("status", "SETTLED"),
        amount_cents=data["amount_cents"],
        description=(data.get("description") or "")[:200],
        message=(data.get("message") or "")[:280],
        raw_text=(data.get("raw_text") or "")[:280],
        created_at=data["created_at"],
        is_simulated=simulated,
    )
    s.add(tx)
    s.flush()
    decision, chosen, cands, note = evaluate(s, tx)
    tx.candidates = [c.as_dict() for c in cands]
    tx.match_note = note
    if decision == "auto" and chosen is not None:
        _confirm(s, tx, chosen.participant, "up_auto" if not simulated else "simulated", note, "system")
        tx.match_state = "auto"
    else:
        tx.match_state = decision
        if decision == "review":
            for d in {c.dinner.id: c.dinner for c in cands}.values():
                touch(s, d, "payment_review")
    s.add(tx)
    return tx


def _confirm(s: Session, tx: BankTransaction, p: Participant, source: str, reason: str, actor: str) -> Payment:
    live = s.exec(
        select(Payment).where(Payment.bank_transaction_id == tx.id, Payment.undone_at == None)  # noqa: E711
    ).first()
    if live is not None:
        raise HTTPException(409, "That transfer is already confirming a payment.")
    pay = Payment(
        dinner_id=p.dinner_id,
        participant_id=p.id,
        amount_cents=tx.amount_cents,
        source=source,
        bank_transaction_id=tx.id,
        match_reason=reason,
    )
    s.add(pay)
    dinner = s.get(Dinner, p.dinner_id)
    audit(s, dinner.id, actor, f"Payment {fmt(tx.amount_cents)} from {p.display_name}: {reason}")
    touch(s, dinner, "payment")
    return pay


def mark_deleted(s: Session, tx_id: str) -> None:
    tx = s.get(BankTransaction, tx_id)
    if tx is None:
        return
    tx.status = "DELETED"
    _reverse(s, tx)
    s.add(tx)


def _reverse(s: Session, tx: BankTransaction) -> None:
    for pay in s.exec(
        select(Payment).where(Payment.bank_transaction_id == tx.id, Payment.undone_at == None)  # noqa: E711
    ).all():
        pay.undone_at = utcnow()
        pay.undo_reason = "Up removed this pending transaction"
        s.add(pay)
        touch(s, s.get(Dinner, pay.dinner_id), "payment")
    tx.match_state = "ignored"
    tx.match_note = "Removed by Up before settling."


# ------------------------------------------------------------ manual steps
def confirm_transaction(s: Session, tx_id: str, participant_id: str) -> Payment:
    tx = s.get(BankTransaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Transfer not found.")
    if tx.status == "DELETED":
        raise HTTPException(409, "Up removed this transfer.")
    p = s.get(Participant, participant_id)
    if p is None or p.removed:
        raise HTTPException(404, "Person not found.")
    dinner = s.get(Dinner, p.dinner_id)
    if dinner.is_demo != tx.is_simulated:
        raise HTTPException(409, "Simulated transfers can only pay demo dinners, and real ones only real dinners.")
    pay = _confirm(
        s,
        tx,
        p,
        "up_review" if not tx.is_simulated else "simulated",
        f"Confirmed by organiser ({tx.match_note})" if tx.match_note else "Confirmed by organiser",
        "organiser",
    )
    tx.match_state = "confirmed"
    s.add(tx)
    return pay


def ignore_transaction(s: Session, tx_id: str) -> None:
    tx = s.get(BankTransaction, tx_id)
    if tx is None:
        raise HTTPException(404, "Transfer not found.")
    if tx.match_state in ("auto", "confirmed"):
        raise HTTPException(409, "Undo the payment first.")
    tx.match_state = "ignored"
    s.add(tx)


def record_manual(s: Session, dinner: Dinner, participant_id: str, amount_cents: int, note: str) -> Payment:
    p = s.get(Participant, participant_id)
    if p is None or p.dinner_id != dinner.id:
        raise HTTPException(404, "Person not found.")
    if amount_cents == 0 or abs(amount_cents) > 100_000_00:
        raise HTTPException(422, "Enter the amount received.")
    pay = Payment(
        dinner_id=dinner.id,
        participant_id=p.id,
        amount_cents=amount_cents,
        source="manual",
        match_reason=note.strip()[:200] or "Recorded manually by organiser",
    )
    s.add(pay)
    audit(s, dinner.id, "organiser", f"Manual payment {fmt(amount_cents)} for {p.display_name}")
    touch(s, dinner, "payment")
    return pay


def undo_payment(s: Session, payment_id: str, reason: str) -> None:
    pay = s.get(Payment, payment_id)
    if pay is None or pay.undone_at is not None:
        raise HTTPException(404, "Payment not found or already undone.")
    pay.undone_at = utcnow()
    pay.undo_reason = reason.strip()[:200] or "Undone by organiser"
    s.add(pay)
    if pay.bank_transaction_id:
        tx = s.get(BankTransaction, pay.bank_transaction_id)
        if tx is not None:
            tx.match_state = "review"
            tx.match_note = f"Payment undone: {pay.undo_reason}"
            s.add(tx)
    dinner = s.get(Dinner, pay.dinner_id)
    audit(s, dinner.id, "organiser", f"Undid payment {fmt(pay.amount_cents)}: {pay.undo_reason}")
    touch(s, dinner, "payment")


def mark_sent(s: Session, dinner: Dinner, p: Participant, sent: bool) -> None:
    if dinner.instructions_issued_at is None:
        raise HTTPException(409, "Payment details aren't out yet.")
    p.marked_sent_at = utcnow() if sent else None
    s.add(p)
    audit(s, dinner.id, p.display_name, "Marked payment sent" if sent else "Unmarked payment sent")
    touch(s, dinner, "payment")


def payments_for(s: Session, dinner_id: str) -> list[Payment]:
    return list(s.exec(select(Payment).where(Payment.dinner_id == dinner_id).order_by(col(Payment.created_at))).all())


def review_queue(s: Session, *, include_unmatched: bool = True) -> list[BankTransaction]:
    states = ["review"] + (["unmatched"] if include_unmatched else [])
    return list(
        s.exec(
            select(BankTransaction)
            .where(col(BankTransaction.match_state).in_(states))
            .order_by(col(BankTransaction.created_at).desc())
            .limit(100)
        ).all()
    )
