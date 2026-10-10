"""Recording, sharing, quantities, concurrency, idempotency and sessions."""

import threading

from tests.conftest import H, new_client


def test_shortlist_never_creates_a_charge(dinner):
    item = dinner.add_item("Steak", 4200)
    helen = dinner.guest("Helen")
    r = helen.post(f"{dinner.base}/shortlist", json={"menu_item_id": item, "on": True}, headers=H)
    assert r.status_code == 200
    st = dinner.gstate(helen)
    assert st["shortlist"] == [item]
    assert st["lines"] == [] and st["bill"]["recorded_total"] == 0
    # …and it's private to Helen.
    tom = dinner.guest("Tom")
    assert dinner.gstate(tom)["shortlist"] == []


def test_prices_come_from_the_menu_not_the_phone(dinner):
    item = dinner.add_item(
        "Wine",
        None,
        variants=[{"label": "Glass", "price_cents": 1100}, {"label": "Bottle", "price_cents": 4800}],
        extras=[{"label": "Ice", "price_cents": 50}],
    )
    helen = dinner.guest("Helen")
    line = dinner.record(
        helen, menu_item_id=item, variant_label="Bottle", extras=["Ice"], quantity=2, unit_price_cents=1
    )["line"]
    assert line["unit_price_cents"] == 4800
    assert dinner.state()["bill"]["lines"][line["id"]]["amount"] == (4800 + 50) * 2
    bad = helen.post(f"{dinner.base}/lines", json={"menu_item_id": item, "variant_label": "Magnum"}, headers=H)
    assert bad.status_code == 422
    missing = helen.post(f"{dinner.base}/lines", json={"menu_item_id": item}, headers=H)
    assert missing.status_code == 422


def test_unpriced_dish_takes_guest_price_and_is_flagged_until_priced(dinner):
    item = dinner.add_item("Market fish", None)
    helen = dinner.guest("Helen")
    line = dinner.record(helen, menu_item_id=item)["line"]
    assert any(i["code"] == "no_price" for i in dinner.state()["bill"]["issues"])
    helen.patch(
        f"{dinner.base}/lines/{line['id']}", json={"version": line["version"], "unit_price_cents": 3900}, headers=H
    )
    assert not any(i["code"] == "no_price" for i in dinner.state()["bill"]["issues"])


def test_quantities_notes_edit_and_remove(dinner):
    item = dinner.add_item("Beer", 1000)
    tom = dinner.guest("Tom")
    line = dinner.record(tom, menu_item_id=item, quantity=3, note="cold please")["line"]
    assert dinner.state()["bill"]["people"][tom.pid]["items"] == 3000
    r = tom.patch(f"{dinner.base}/lines/{line['id']}", json={"version": line["version"], "quantity": 2}, headers=H)
    assert r.status_code == 200
    assert dinner.state()["bill"]["people"][tom.pid]["items"] == 2000
    v = r.json()["line"]["version"]
    assert tom.delete(f"{dinner.base}/lines/{line['id']}?version={v}", headers=H).status_code == 200
    assert dinner.state()["lines"] == []


def test_someone_else_cannot_edit_or_delete_your_line(dinner):
    item = dinner.add_item("Beer", 1000)
    tom, helen = dinner.guest("Tom"), dinner.guest("Helen")
    line = dinner.record(tom, menu_item_id=item)["line"]
    r = helen.patch(f"{dinner.base}/lines/{line['id']}", json={"version": line["version"], "quantity": 9}, headers=H)
    assert r.status_code == 403
    assert helen.delete(f"{dinner.base}/lines/{line['id']}?version={line['version']}", headers=H).status_code == 403


def test_shared_dish_equal_split_and_explicit_join(dinner):
    naan = dinner.add_item("Garlic naan", 1000)
    a, b, c = dinner.guest("Ann"), dinner.guest("Bob"), dinner.guest("Cat")
    line = dinner.record(a, menu_item_id=naan, shared=True, participants=[a.pid, b.pid])["line"]
    assert dinner.state()["bill"]["lines"][line["id"]]["shares"] == {a.pid: 500, b.pid: 500}

    # Cat taps "Record" on the same dish: she's asked to join, not given a duplicate.
    r = dinner.record(c, menu_item_id=naan)
    assert "exists" in r and r["exists"][0]["id"] == line["id"]
    assert len(dinner.state()["lines"]) == 1

    # Joining is explicit and shows up for everyone.
    j = c.post(f"{dinner.base}/lines/{line['id']}/join", json={"version": line["version"]}, headers=H)
    assert j.status_code == 200
    shares = dinner.state()["bill"]["lines"][line["id"]]["shares"]
    assert shares == {a.pid: 334, b.pid: 333, c.pid: 333}

    # Or she really did order her own.
    own = dinner.record(c, menu_item_id=naan, separate=True)
    assert "line" in own and len(dinner.state()["lines"]) == 2


def test_join_with_stale_view_is_refused_not_applied_silently(dinner):
    naan = dinner.add_item("Garlic naan", 1200)
    a, b, c = dinner.guest("Ann"), dinner.guest("Bob"), dinner.guest("Cat")
    line = dinner.record(a, menu_item_id=naan, shared=True, participants=[a.pid])["line"]
    seen_version = line["version"]  # both Bob and Cat are looking at this
    assert (
        b.post(f"{dinner.base}/lines/{line['id']}/join", json={"version": seen_version}, headers=H).status_code == 200
    )
    # Cat's confirmation screen showed "Ann $6, you $6" — that's no longer true.
    r = c.post(f"{dinner.base}/lines/{line['id']}/join", json={"version": seen_version}, headers=H)
    assert r.status_code == 409
    assert dinner.state()["bill"]["lines"][line["id"]]["shares"] == {a.pid: 600, b.pid: 600}


def test_unequal_shares_and_organiser_correction(dinner):
    item = dinner.add_item("Platter", 3000)
    a, b = dinner.guest("Ann"), dinner.guest("Bob")
    line = dinner.record(a, menu_item_id=item, shared=True, participants=[a.pid, b.pid])["line"]
    r = dinner.org.put(
        f"{dinner.base}/lines/{line['id']}/allocations",
        headers=H,
        json={
            "version": line["version"],
            "split_mode": "shares",
            "allocations": [{"participant_id": a.pid, "weight": 2}, {"participant_id": b.pid, "weight": 1}],
        },
    )
    assert r.status_code == 200, r.text
    assert dinner.state()["bill"]["lines"][line["id"]]["shares"] == {a.pid: 2000, b.pid: 1000}
    # A guest can't reallocate someone else's line.
    r2 = b.put(
        f"{dinner.base}/lines/{line['id']}/allocations",
        headers=H,
        json={"version": r.json()["line"]["version"], "allocations": [{"participant_id": a.pid}]},
    )
    assert r2.status_code == 403


def test_units_claimed_from_a_quantity_line(dinner):
    beer = dinner.add_item("Pint", 1200)
    tom, derek_view, priya = dinner.guest("Tom"), dinner.guest("Dee"), dinner.guest("Priya")
    line = dinner.record(
        tom,
        menu_item_id=beer,
        quantity=3,
        shared=True,
        split_mode="units",
        participants=[tom.pid],
        weights={tom.pid: 2},
    )["line"]
    calc = dinner.state()["bill"]["lines"][line["id"]]
    assert calc["shares"] == {tom.pid: 2400} and calc["unallocated"] == 1200
    r = derek_view.post(
        f"{dinner.base}/lines/{line['id']}/join", json={"version": line["version"], "units": 1}, headers=H
    )
    assert r.status_code == 200
    # All three claimed: Priya can't claim a fourth.
    r = priya.post(
        f"{dinner.base}/lines/{line['id']}/join", json={"version": r.json()["line"]["version"], "units": 1}, headers=H
    )
    assert r.status_code == 409
    calc = dinner.state()["bill"]["lines"][line["id"]]
    assert calc["unallocated"] == 0 and sum(calc["shares"].values()) == 3600


def test_simultaneous_joins_do_not_both_apply(dinner):
    item = dinner.add_item("Dumplings", 1800)
    a = dinner.guest("Ann")
    line = dinner.record(a, menu_item_id=item, shared=True, participants=[a.pid])["line"]
    guests = [dinner.guest(f"G{i}") for i in range(6)]
    results = []

    def go(c):
        results.append(
            c.post(f"{dinner.base}/lines/{line['id']}/join", json={"version": line["version"]}, headers=H).status_code
        )

    threads = [threading.Thread(target=go, args=(c,)) for c in guests]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] + [409] * 5
    assert len(dinner.line(line["id"])["allocations"]) == 2


def test_retry_after_dropped_reply_does_not_duplicate(dinner):
    item = dinner.add_item("Beer", 1000)
    tom = dinner.guest("Tom")
    body = {"menu_item_id": item, "op_id": "a1b2c3d4e5f6"}
    first = dinner.record(tom, **body)
    again = dinner.record(tom, **body)
    assert first == again and len(dinner.state()["lines"]) == 1
    line = first["line"]
    j = {"version": line["version"], "op_id": "join-op-1"}
    helen = dinner.guest("Helen")
    helen.post(f"{dinner.base}/lines/{line['id']}/join", json=j, headers=H)
    retry = helen.post(f"{dinner.base}/lines/{line['id']}/join", json=j, headers=H)
    assert retry.status_code == 200  # replayed, not "already sharing"


def test_session_survives_and_names_cannot_impersonate(dinner):
    helen = dinner.guest("Helen")
    item = dinner.add_item("Soup", 900)
    dinner.record(helen, menu_item_id=item)
    # Same phone later (e.g. back from the banking app): still Helen.
    assert dinner.gstate(helen)["me"]["name"] == "Helen"
    # A stranger who knows her name just becomes a second "Helen".
    impostor = dinner.guest("Helen")
    st = dinner.gstate(impostor)
    assert st["me"]["id"] != helen.pid and st["me"]["reference"] != helen.reference
    assert st["bill"]["people"][st["me"]["id"]]["total"] == 0
    # Joining twice on one phone is refused.
    assert helen.post(f"{dinner.base}/join", json={"name": "Helen"}, headers=H).status_code == 409


def test_recovery_link_moves_a_guest_to_a_new_phone_once(dinner):
    helen = dinner.guest("Helen")
    r = dinner.org.post(f"/api/o/d/{dinner.id}/people/{helen.pid}/link", headers=H)
    path = r.json()["url"].split("testserver", 1)[-1]
    new_phone = new_client()
    assert new_phone.get(path, follow_redirects=False).status_code == 303
    assert dinner.gstate(new_phone)["me"]["id"] == helen.pid
    assert new_client().get(path, follow_redirects=False).status_code == 404  # single use


def test_guests_see_only_their_own_reference_and_payment(dinner):
    helen, tom = dinner.guest("Helen"), dinner.guest("Tom")
    st = dinner.gstate(helen)
    assert st["me"]["reference"] == helen.reference
    assert all("reference" not in p for p in st["participants"])
    assert tom.reference not in str(st)
    for private in ("payments", "bank_review", "payment_statuses", "audit", "receipt"):
        assert private not in st


def test_rename_keeps_reference(dinner):
    helen = dinner.guest("Helen")
    helen.post(f"{dinner.base}/me", json={"name": "Helena"}, headers=H)
    st = dinner.gstate(helen)
    assert st["me"]["name"] == "Helena" and st["me"]["reference"] == helen.reference


def test_duplicate_names_get_distinct_references(dinner):
    refs = [dinner.guest(n).reference for n in ("Helen", "Helen", "Helen G", "helen")]
    code = dinner.state()["dinner"]["code"]
    assert refs == [f"{code}-HELEN", f"{code}-HELEN2", f"{code}-HELENG", f"{code}-HELEN3"]


def test_requests_without_app_header_are_refused(dinner):
    helen = dinner.guest("Helen")
    assert helen.post(f"{dinner.base}/lines", json={"name": "x", "unit_price_cents": 1}).status_code == 403


def test_unknown_dinner_link(client):
    assert client.get("/api/d/not-a-real-token/state").status_code == 404


def test_table_number_is_kept_tidy_and_shown_to_guests(organiser):
    from tests.conftest import Dinner as D

    r = organiser.post("/api/o/dinners", json={"restaurant_name": "Bistro", "table_label": " Table  12 "}, headers=H)
    d = D(organiser, r.json()["id"])
    assert d.state()["dinner"]["table_label"] == "12"
    organiser.patch(f"/api/o/d/{d.id}", json={"table_label": "Courtyard 3"}, headers=H)
    assert d.gstate(d.guest("Pat"))["dinner"]["table_label"] == "Courtyard 3"
    listed = organiser.get("/api/o/dinners").json()["dinners"]
    assert listed[0]["table_label"] == "Courtyard 3"
