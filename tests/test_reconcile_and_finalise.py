"""Receipt reconciliation (no double counting), quoted totals, finalisation."""

from app.services.demo import receipt_image
from app.services.extraction import CannedExtractor, ExtractedReceiptLine, ReceiptExtraction
from app.services.images import normalise
from tests.conftest import H, set_profile


def rl(desc, qty, total, kind="item"):
    return ExtractedReceiptLine(
        description=desc,
        quantity=qty,
        unit_price_cents=total // qty if kind == "item" else None,
        line_total_cents=total,
        kind=kind,
        uncertain=False,
        note="",
    )


def scan(dinner, result: ReceiptExtraction):
    CannedExtractor.register_receipt(normalise(receipt_image()), result)
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/receipt", files=[("files", ("r.png", receipt_image(), "image/png"))], headers=H
    )
    assert r.status_code == 200, r.text
    return dinner.state()["receipt"]


def rline(receipt, desc):
    return next(x for x in receipt["lines"] if x["description"] == desc)


def setup_menu_first(dinner):
    naan = dinner.add_item("Garlic Naan", 600)
    lamb = dinner.add_item("Lamb Rogan Josh", 2800)
    wine = dinner.add_item("House Red Glass", 1100)
    a, b = dinner.guest("Ann"), dinner.guest("Bob")
    shared = dinner.record(a, menu_item_id=naan, quantity=2, shared=True, participants=[a.pid, b.pid])["line"]
    dinner.record(a, menu_item_id=lamb)
    dinner.record(b, menu_item_id=wine)
    return a, b, shared


RECEIPT = ReceiptExtraction(
    lines=[
        rl("2 Garlic Naan", 2, 1200),
        rl("1 LAMB RGN JSH", 1, 2800),
        rl("1 House Red Glass", 1, 1200),
        rl("1 Mango Lassi", 1, 750),
        rl("SUBTOTAL", 1, 5950, "subtotal"),
        rl("Surcharge 10%", 1, 595, "surcharge"),
        rl("TOTAL", 1, 6545, "total"),
        rl("GST included", 1, 595, "tax_info"),
    ],
    subtotal_cents=5950,
    total_cents=6545,
    gst_cents=595,
    gst_included=True,
    notes=[],
)


def test_receipt_matching_preserves_allocations_and_never_double_counts(dinner):
    a, b, shared = setup_menu_first(dinner)
    rec = scan(dinner, RECEIPT)
    st = dinner.state()
    # Exact match confirmed automatically, keeping Ann & Bob's split.
    naan = rline(rec, "2 Garlic Naan")
    assert naan["state"] == "matched" and naan["matched_line_ids"] == [shared["id"]]
    # Abbreviation needs a person; changed price needs a person.
    assert rline(rec, "1 LAMB RGN JSH")["state"] == "pending"
    assert any("abbreviated" in m for m in rline(rec, "1 LAMB RGN JSH")["suggestion"]["reasons"])
    wine = rline(rec, "1 House Red Glass")
    assert wine["state"] == "pending" and any("Price differs" in m for m in wine["suggestion"]["reasons"])
    # GST is informational only; total taken from the receipt.
    assert rline(rec, "GST included")["state"] == "ignored"
    assert st["dinner"]["bill_total_cents"] == 6545
    recorded_before = st["bill"]["recorded_total"]

    base = f"/api/o/d/{dinner.id}/receipt/lines"
    lamb = rline(rec, "1 LAMB RGN JSH")
    assert (
        dinner.org.post(
            f"{base}/{lamb['id']}/match",
            json={"line_ids": lamb["suggestion"]["line_ids"], "use_receipt": True},
            headers=H,
        ).status_code
        == 200
    )
    assert (
        dinner.org.post(
            f"{base}/{wine['id']}/match",
            json={"line_ids": wine["suggestion"]["line_ids"], "use_receipt": True},
            headers=H,
        ).status_code
        == 200
    )
    st = dinner.state()
    # Matching changed the wine to the receipt price but added nothing.
    assert len(st["lines"]) == 3
    assert st["bill"]["recorded_total"] == recorded_before + 100
    assert st["bill"]["lines"][shared["id"]]["shares"] == {a.pid: 600, b.pid: 600}

    # Matching the same recorded line twice is refused.
    lassi = rline(rec, "1 Mango Lassi")
    assert (
        dinner.org.post(f"{base}/{lassi['id']}/match", json={"line_ids": [shared["id"]]}, headers=H).status_code == 409
    )
    # The extra item is added once, for someone to claim; the surcharge is added proportionally.
    assert dinner.org.post(f"{base}/{lassi['id']}/add", headers=H).status_code == 200
    assert dinner.org.post(f"{base}/{lassi['id']}/add", headers=H).status_code == 409
    surcharge = rline(rec, "Surcharge 10%")
    assert dinner.org.post(f"{base}/{surcharge['id']}/add", headers=H).status_code == 200
    st = dinner.state()
    assert st["bill"]["recorded_total"] == 6545 == st["bill"]["bill_total"]
    lassi_line = next(ln for ln in st["lines"] if ln["name"] == "1 Mango Lassi")
    assert st["bill"]["lines"][lassi_line["id"]]["unallocated"] == 750
    assert not st["bill"]["accounted_for"]
    b.post(f"{dinner.base}/lines/{lassi_line['id']}/join", json={"version": lassi_line["version"]}, headers=H)
    st = dinner.state()
    assert st["bill"]["accounted_for"], st["bill"]["issues"]
    assert sum(p["total"] for p in st["bill"]["people"].values()) == 6545
    # Undoing a match returns it to review.
    assert dinner.org.post(f"{base}/{lamb['id']}/reset", headers=H).status_code == 200
    assert not dinner.state()["bill"]["accounted_for"]


def test_recorded_item_missing_from_receipt_must_be_resolved(dinner):
    a, b, _ = setup_menu_first(dinner)
    extra = dinner.add_item("Chicken Tikka", 1400)
    tikka = dinner.record(a, menu_item_id=extra)["line"]
    scan(dinner, RECEIPT)
    issues = dinner.state()["bill"]["issues"]
    assert any(i["code"] == "not_on_receipt" and i["line_id"] == tikka["id"] for i in issues)
    dinner.org.patch(
        f"{dinner.base}/lines/{tikka['id']}", json={"version": tikka["version"], "not_on_receipt_ok": True}, headers=H
    )
    issues = dinner.state()["bill"]["issues"]
    assert not any(i.get("line_id") == tikka["id"] and i["code"] == "not_on_receipt" for i in issues)


def test_receipt_first_add_all_then_guests_claim(dinner):
    a, b = dinner.guest("Ann"), dinner.guest("Bob")
    receipt = ReceiptExtraction(
        lines=[rl("PIZZA MARG", 1, 2400), rl("COKE", 2, 900), rl("TOTAL", 1, 3300, "total")],
        subtotal_cents=None,
        total_cents=3300,
        gst_cents=None,
        gst_included=None,
        notes=[],
    )
    scan(dinner, receipt)
    assert dinner.org.post(f"/api/o/d/{dinner.id}/receipt/add-all", headers=H).status_code == 200
    st = dinner.state()
    assert st["bill"]["recorded_total"] == 3300 and st["bill"]["unallocated_total"] == 3300
    pizza = next(ln for ln in st["lines"] if ln["name"] == "PIZZA MARG")
    coke = next(ln for ln in st["lines"] if ln["name"] == "COKE")
    assert coke["split_mode"] == "units"
    a.post(f"{dinner.base}/lines/{pizza['id']}/join", json={"version": pizza["version"]}, headers=H)
    b.post(f"{dinner.base}/lines/{pizza['id']}/join", json={"version": pizza["version"] + 1}, headers=H)
    a.post(f"{dinner.base}/lines/{coke['id']}/join", json={"version": coke["version"], "units": 1}, headers=H)
    b.post(f"{dinner.base}/lines/{coke['id']}/join", json={"version": coke["version"] + 1, "units": 1}, headers=H)
    bill = dinner.state()["bill"]
    assert bill["accounted_for"], bill["issues"]
    assert bill["people"][a.pid]["total"] == 1200 + 450


def test_quoted_total_difference_is_never_spread_silently(dinner):
    set_profile()
    item = dinner.add_item("Pasta", 2500)
    a, b = dinner.guest("Ann"), dinner.guest("Bob")
    dinner.record(a, menu_item_id=item)
    dinner.record(b, menu_item_id=item, separate=True)
    st = dinner.state()
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": 5300}, headers=H)
    st = dinner.state()
    assert st["bill"]["difference"] == 300 and not st["bill"]["accounted_for"]
    assert sum(p["total"] for p in st["bill"]["people"].values()) == 5000  # nothing spread
    r = dinner.org.post(f"/api/o/d/{dinner.id}/finalise", json={"revision": st["dinner"]["revision"]}, headers=H)
    assert r.status_code == 409
    # An explicit, labelled adjustment resolves it.
    dinner.org.post(
        f"{dinner.base}/lines",
        headers=H,
        json={
            "name": "Bread service (not on menu)",
            "kind": "adjustment",
            "unit_price_cents": 300,
            "total_cents": 300,
            "split_mode": "equal",
            "participants": [a.pid, b.pid],
            "shared": True,
        },
    )
    st = dinner.state()
    assert st["bill"]["accounted_for"]
    assert st["bill"]["people"][a.pid]["total"] == 2650


def test_finalise_locks_and_organiser_owes_nothing(dinner):
    set_profile()
    item = dinner.add_item("Pasta", 2000)
    a = dinner.guest("Ann")
    me = dinner.state()["dinner"]
    derek = next(p for p in dinner.state()["participants"] if p["is_organiser"])
    dinner.record(a, menu_item_id=item)
    dinner.record(dinner.org, menu_item_id=item, separate=True, participants=[derek["id"]])
    st = dinner.state()
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": 4000}, headers=H)
    st = dinner.state()
    r = dinner.org.post(f"/api/o/d/{dinner.id}/finalise", json={"revision": st["dinner"]["revision"]}, headers=H)
    assert r.status_code == 200, r.text
    ann = dinner.gstate(a)
    assert ann["payment"]["state"] == "awaiting" and ann["payment"]["share"] == 2000
    assert ann["payment"]["instructions"]["reference"] == a.reference
    assert ann["payment"]["instructions"]["payid"] == "0400000000"
    mine = dinner.state()["payment_statuses"][derek["id"]]
    assert mine["state"] == "self"
    # Locked.
    assert a.post(f"{dinner.base}/lines", json={"menu_item_id": item, "separate": True}, headers=H).status_code == 423
    assert me["status"] == "open"


def test_finalise_refused_when_someone_changed_something(dinner):
    set_profile()
    item = dinner.add_item("Pasta", 2000)
    a = dinner.guest("Ann")
    dinner.record(a, menu_item_id=item)
    st = dinner.state()
    dinner.org.post(f"/api/o/d/{dinner.id}/total", json={"version": st["dinner"]["version"], "cents": 2000}, headers=H)
    seen = dinner.state()["dinner"]["revision"]
    a.post(f"{dinner.base}/shortlist", json={"menu_item_id": item, "on": True}, headers=H)  # doesn't bump the bill
    dinner.guest("Late arrival")  # does change the table
    r = dinner.org.post(f"/api/o/d/{dinner.id}/finalise", json={"revision": seen}, headers=H)
    assert r.status_code == 409
