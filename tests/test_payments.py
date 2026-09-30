"""Payment status, Up webhooks and matching, manual confirmation, reopening.

Up is faked with httpx.MockTransport using the payload shapes from Up's
published OpenAPI spec; nothing touches the network.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.db import locked_write, session_scope
from app.models import BankTransaction, Payment
from app.security import encrypt
from app.services import payments, up
from tests.conftest import H, set_profile

SECRET = "up-webhook-secret"
ACCOUNT = "acc-transactional"


# ------------------------------------------------------------------ helpers
def finalised(dinner, prices: dict[str, int], total_extra: int = 0):
    """Guests each order one priced item; bill finalised. Returns {name: client}."""
    set_profile()
    guests = {}
    for name, cents in prices.items():
        c = dinner.guest(name)
        item = dinner.add_item(f"{name}'s dish", cents)
        dinner.record(c, menu_item_id=item)
        guests[name] = c
    st = dinner.state()
    total = sum(prices.values()) + total_extra
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": total}, headers=H)
    st = dinner.state()
    r = dinner.org.post(f"/api/o/d/{dinner.id}/finalise", json={"revision": st["dinner"]["revision"]}, headers=H)
    assert r.status_code == 200, r.text
    return guests


def tx(amount, message="", description="", created=None, tx_id=None, account=ACCOUNT, status="SETTLED"):
    created = created or datetime.now(UTC) + timedelta(minutes=1)
    return {
        "type": "transactions",
        "id": tx_id or f"tx-{amount}-{message}-{description}",
        "attributes": {
            "status": status,
            "rawText": None,
            "description": description,
            "message": message or None,
            "amount": {"currencyCode": "AUD", "value": f"{amount / 100:.2f}", "valueInBaseUnits": amount},
            "createdAt": created.isoformat(),
            "settledAt": None,
        },
        "relationships": {"account": {"data": {"type": "accounts", "id": account}}},
    }


def ingest(t: dict, simulated=False):
    with locked_write() as s:
        result = payments.ingest(s, up.normalise(t), simulated=simulated)
        return (result.match_state, result.match_note) if result else (None, None)


def status(dinner, client):
    return dinner.gstate(client)["payment"]


@pytest.fixture
def up_fake(monkeypatch):
    store: dict[str, dict] = {}
    calls = {"n": 0}

    def handler(request: httpx.Request):
        calls["n"] += 1
        path = request.url.path.removeprefix("/api/v1")
        if path.startswith("/transactions/"):
            t = store.get(path.split("/")[-1])
            return httpx.Response(200, json={"data": t}) if t else httpx.Response(404, json={})
        if path == f"/accounts/{ACCOUNT}/transactions":
            return httpx.Response(200, json={"data": list(store.values()), "links": {"prev": None, "next": None}})
        return httpx.Response(404, json={})

    real = up.UpClient
    monkeypatch.setattr(
        up, "UpClient", lambda *a, **k: real(token="test-token", transport=httpx.MockTransport(handler))
    )
    with locked_write() as s:
        up.put_setting(s, "up_webhook_secret", encrypt(SECRET))
        up.put_setting(s, "up_account_id", ACCOUNT)
    return store, calls


def post_event(client, event_id, etype, tx_id=None, secret=SECRET):
    body = {
        "data": {
            "type": "webhook-events",
            "id": event_id,
            "attributes": {"eventType": etype, "createdAt": datetime.now(UTC).isoformat()},
            "relationships": {"webhook": {"data": {"type": "webhooks", "id": "wh1"}}},
        }
    }
    if tx_id:
        body["data"]["relationships"]["transaction"] = {"data": {"type": "transactions", "id": tx_id}}
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/up", content=raw, headers={"X-Up-Authenticity-Signature": sig, "Content-Type": "application/json"}
    )


# ----------------------------------------------------------------- matching
def test_reference_and_exact_amount_confirms_automatically(dinner):
    g = finalised(dinner, {"Helen": 2350, "Tom": 3100})
    state, note = ingest(tx(2350, message=g["Helen"].reference))
    assert state == "auto" and "Reference" in note
    p = status(dinner, g["Helen"])
    assert p["state"] == "confirmed" and p["balance"] == 0
    assert status(dinner, g["Tom"])["state"] == "awaiting"


def test_marked_sent_is_not_received(dinner):
    g = finalised(dinner, {"Helen": 2350})
    g["Helen"].post(f"{dinner.base}/sent", json={"sent": True}, headers=H)
    p = status(dinner, g["Helen"])
    assert p["state"] == "marked_sent" and p["received"] == 0


def test_unique_exact_amount_without_reference(dinner):
    g = finalised(dinner, {"Helen": 2350, "Tom": 3100})
    state, note = ingest(tx(3100, description="T SMITH"))
    assert state == "auto" and "no reference" in note
    assert status(dinner, g["Tom"])["state"] == "confirmed"


def test_duplicate_amounts_go_to_review(dinner):
    g = finalised(dinner, {"Helen": 2500, "Tom": 2500})
    state, note = ingest(tx(2500))
    assert state == "review" and "2 people owe" in note
    assert status(dinner, g["Helen"])["state"] == "awaiting"


def test_overlapping_dinners_amount_only_goes_to_review(organiser, dinner):
    finalised(dinner, {"Helen": 2500})
    from tests.conftest import Dinner

    other = Dinner(organiser, organiser.post("/api/o/dinners", json={"restaurant_name": "Two"}, headers=H).json()["id"])
    finalised(other, {"Tom": 4100})
    state, note = ingest(tx(4100))
    assert state == "review" and "Several dinners" in note


def test_reference_with_wrong_amount_goes_to_review(dinner):
    g = finalised(dinner, {"Helen": 2350})
    state, _ = ingest(tx(2000, message=g["Helen"].reference))
    assert state == "review"
    assert status(dinner, g["Helen"])["received"] == 0


def test_helen_vs_helen_g_is_not_guessed(dinner):
    g = finalised(dinner, {"Helen": 2000, "Helen G": 2000})
    # "DIN1-HELEN G" could be Helen G typing a space, or Helen adding "G…". Same amount: review.
    state, _ = ingest(tx(2000, message=f"{g['Helen'].reference} G"))
    assert state == "review"


def test_second_helen_with_full_reference_is_not_confused_with_first(dinner):
    g = finalised(dinner, {"Helen": 2000, "Helen G": 2000})
    helen2 = dinner.guest("Helen")  # joins late: DIN…-HELEN2
    assert helen2.reference.endswith("HELEN2")
    # Give Helen2 a share equal to the others, then refinalise.
    v = dinner.state()["dinner"]["version"]
    dinner.org.post(f"/api/o/d/{dinner.id}/reopen", json={"version": v, "acknowledged": True}, headers=H)
    item = dinner.add_item("Helen2 dish", 2000)
    dinner.record(helen2, menu_item_id=item)
    st = dinner.state()
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": 6000}, headers=H)
    st = dinner.state()
    assert (
        dinner.org.post(
            f"/api/o/d/{dinner.id}/finalise", json={"revision": st["dinner"]["revision"]}, headers=H
        ).status_code
        == 200
    )
    state, _ = ingest(tx(2000, message=helen2.reference))
    assert state == "auto"
    assert status(dinner, helen2)["state"] == "confirmed"
    assert status(dinner, g["Helen"])["state"] == "awaiting"


def test_similar_reference_resolved_by_amount(dinner):
    g = finalised(dinner, {"Helen": 2000, "Helen G": 2750})
    state, note = ingest(tx(2750, message=g["Helen G"].reference))
    assert state == "auto"
    assert status(dinner, g["Helen G"])["state"] == "confirmed"


def test_transfer_before_instructions_or_debits_are_ignored(dinner):
    finalised(dinner, {"Helen": 2000})
    assert ingest(tx(-2000))[0] is None
    long_ago = datetime.now(UTC) - timedelta(days=2)
    assert ingest(tx(2000, created=long_ago, tx_id="old"))[0] is None
    with session_scope() as s:
        assert s.get(BankTransaction, "old") is None  # not even stored


def test_one_transfer_confirms_at_most_one_payment(dinner):
    g = finalised(dinner, {"Helen": 2000, "Tom": 3000})
    ingest(tx(2000, message=g["Helen"].reference, tx_id="T1"))
    ingest(tx(2000, message=g["Helen"].reference, tx_id="T1"))  # seen again
    with session_scope() as s:
        live = [p for p in payments.payments_for(s, dinner.id) if not p.undone_at]
    assert len(live) == 1
    # And the organiser can't also assign it to Tom.
    r = dinner.org.post("/api/o/tx/T1/confirm", json={"participant_id": g["Tom"].pid}, headers=H)
    assert r.status_code == 409


def test_manual_confirmation_correction_and_undo(dinner):
    g = finalised(dinner, {"Helen": 2500, "Tom": 2500})
    ingest(tx(2500, tx_id="T9"))  # ambiguous -> review
    r = dinner.org.post("/api/o/tx/T9/confirm", json={"participant_id": g["Tom"].pid}, headers=H)
    assert r.status_code == 200
    assert status(dinner, g["Tom"])["state"] == "confirmed"
    pay_id = r.json()["id"]
    # Wrong person: undo keeps history and puts the transfer back in review.
    assert dinner.org.post(f"/api/o/payments/{pay_id}/undo", json={"reason": "was Helen"}, headers=H).status_code == 200
    assert status(dinner, g["Tom"])["state"] == "awaiting"
    assert (
        dinner.org.post("/api/o/tx/T9/confirm", json={"participant_id": g["Helen"].pid}, headers=H).status_code == 200
    )
    assert status(dinner, g["Helen"])["state"] == "confirmed"
    hist = dinner.state()["payments"]
    assert len(hist) == 2 and hist[0]["undone_at"] and hist[0]["undo_reason"] == "was Helen"
    # Cash, recorded by hand, with no Up connection at all.
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/payments",
        json={"participant_id": g["Tom"].pid, "amount_cents": 2500, "note": "cash"},
        headers=H,
    )
    assert r.status_code == 200 and status(dinner, g["Tom"])["state"] == "confirmed"


def test_part_payment_and_overpayment_are_shown(dinner):
    g = finalised(dinner, {"Helen": 2500, "Tom": 3000})
    dinner.org.post(
        f"/api/o/d/{dinner.id}/payments", json={"participant_id": g["Helen"].pid, "amount_cents": 1000}, headers=H
    )
    p = status(dinner, g["Helen"])
    assert p["state"] == "part_paid" and p["balance"] == 1500
    dinner.org.post(
        f"/api/o/d/{dinner.id}/payments", json={"participant_id": g["Tom"].pid, "amount_cents": 3500}, headers=H
    )
    assert status(dinner, g["Tom"])["state"] == "overpaid"


def test_reopen_warns_keeps_history_and_shows_new_balance(dinner):
    g = finalised(dinner, {"Helen": 2000, "Tom": 3000})
    ingest(tx(2000, message=g["Helen"].reference))
    v = dinner.state()["dinner"]["version"]
    r = dinner.org.post(f"/api/o/d/{dinner.id}/reopen", json={"version": v}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_reopen"
    assert "1 payment" in r.json()["detail"]["message"]
    assert (
        dinner.org.post(
            f"/api/o/d/{dinner.id}/reopen", json={"version": v, "acknowledged": True}, headers=H
        ).status_code
        == 200
    )
    reopened = status(dinner, g["Helen"])
    assert reopened["instructions"] is None and reopened["received"] == 2000
    # Helen's dish was actually $22: correct it, refinalise, and she owes $2 more.
    st = dinner.state()
    line = next(ln for ln in st["lines"] if ln["name"] == "Helen's dish")
    dinner.org.patch(
        f"{dinner.base}/lines/{line['id']}", json={"version": line["version"], "total_cents": 2200}, headers=H
    )
    st = dinner.state()
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": 5200}, headers=H)
    st = dinner.state()
    assert (
        dinner.org.post(
            f"/api/o/d/{dinner.id}/finalise", json={"revision": st["dinner"]["revision"]}, headers=H
        ).status_code
        == 200
    )
    p = status(dinner, g["Helen"])
    assert p["state"] == "part_paid" and p["balance"] == 200 and p["received"] == 2000


def test_demo_simulation_never_matches_real_dinners(organiser, dinner):
    finalised(dinner, {"Helen": 2000})
    assert ingest(tx(2000), simulated=True)[0] is None  # no demo dinner collecting


# ------------------------------------------------------------------ webhooks
def test_webhook_signature_is_required(client, up_fake):
    assert post_event(client, "ev-bad", "PING", secret="wrong").status_code == 401
    r = client.post("/webhooks/up", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert post_event(client, "ev-ping", "PING").status_code == 200


def test_duplicate_webhook_delivery_is_processed_once(dinner, up_fake):
    store, calls = up_fake
    g = finalised(dinner, {"Helen": 2350})
    t = tx(2350, message=g["Helen"].reference, tx_id="up-tx-1")
    store["up-tx-1"] = t
    for _ in range(3):  # Up retries with the same event id
        assert post_event(dinner.org, "ev-1", "TRANSACTION_CREATED", "up-tx-1").status_code == 200
    # SETTLED for the same transaction: a different event, same transfer.
    assert post_event(dinner.org, "ev-2", "TRANSACTION_SETTLED", "up-tx-1").status_code == 200
    with session_scope() as s:
        pays = [p for p in s.exec(__import__("sqlmodel").select(Payment)).all()]
    assert len(pays) == 1 and pays[0].source == "up_auto"
    assert calls["n"] == 2  # fetched once per distinct event, not per delivery
    assert status(dinner, g["Helen"])["state"] == "confirmed"


def test_webhook_for_other_account_or_debit_is_discarded(dinner, up_fake):
    store, _ = up_fake
    g = finalised(dinner, {"Helen": 2350})
    store["other"] = tx(2350, message=g["Helen"].reference, tx_id="other", account="acc-saver")
    store["debit"] = tx(-2350, tx_id="debit")
    post_event(dinner.org, "ev-o", "TRANSACTION_CREATED", "other")
    post_event(dinner.org, "ev-d", "TRANSACTION_CREATED", "debit")
    with session_scope() as s:
        assert s.get(BankTransaction, "other") is None and s.get(BankTransaction, "debit") is None
    assert status(dinner, g["Helen"])["state"] == "awaiting"


def test_deleted_held_transaction_reverses_payment(dinner, up_fake):
    store, _ = up_fake
    g = finalised(dinner, {"Helen": 2350})
    store["held"] = tx(2350, message=g["Helen"].reference, tx_id="held", status="HELD")
    post_event(dinner.org, "ev-h", "TRANSACTION_CREATED", "held")
    assert status(dinner, g["Helen"])["state"] == "confirmed"
    post_event(dinner.org, "ev-del", "TRANSACTION_DELETED", "held")
    assert status(dinner, g["Helen"])["state"] == "awaiting"
    assert dinner.state()["payments"][0]["undo_reason"].startswith("Up removed")


def test_sync_catches_missed_webhooks(dinner, up_fake):
    store, _ = up_fake
    g = finalised(dinner, {"Helen": 2350, "Tom": 1800})
    store["a"] = tx(2350, message=g["Helen"].reference, tx_id="a")
    store["b"] = tx(1800, description="Tom", tx_id="b")
    store["c"] = tx(-5000, tx_id="c")
    result = up.sync()
    assert result == {"credits_checked": 2}
    assert status(dinner, g["Helen"])["state"] == "confirmed"
    assert status(dinner, g["Tom"])["state"] == "confirmed"
    up.sync()  # again: nothing changes
    with session_scope() as s:
        assert len([p for p in payments.payments_for(s, dinner.id) if not p.undone_at]) == 2


def test_guest_never_receives_bank_data(dinner):
    g = finalised(dinner, {"Helen": 2350, "Tom": 999})
    ingest(tx(4444, message="rent", description="LANDLORD PTY", tx_id="z"))
    st = dinner.gstate(g["Helen"])
    blob = json.dumps(st)
    for secret in ("LANDLORD", "rent", "bank_review", "payments", "4444"):
        assert secret not in blob
