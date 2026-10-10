"""Restaurants remember their menu: saving, starting from it, rescanning."""

from app.services.extraction import CannedExtractor, ExtractedCategory, MenuExtraction
from app.services.images import normalise
from tests.conftest import Dinner, H
from tests.test_menu import _item, png, upload_page


def menu(*items) -> MenuExtraction:
    return MenuExtraction(
        categories=[ExtractedCategory(name="Mains", note="", extras=[], items=list(items))], legend=[], page_notes=[]
    )


def new_dinner(org, name="Test Bistro", **kw) -> Dinner:
    r = org.post("/api/o/dinners", json={"restaurant_name": name, "my_name": "Derek", **kw}, headers=H)
    assert r.status_code == 200, r.text
    return Dinner(org, r.json()["id"])


def scan_and_publish(d: Dinner, colour, result: MenuExtraction) -> str:
    page = upload_page(d, png(colour), result)
    assert d.org.post(f"/api/o/d/{d.id}/pages/{page}/publish", headers=H).status_code == 200
    return page


def names(d: Dinner, *, guest=False) -> dict:
    items = d.gstate(d.guest("Pat"))["menu"]["items"] if guest else d.state()["menu"]["items"]
    return {i["name"]: i for i in items}


def test_next_dinner_starts_with_the_saved_menu(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_text="$40", price_cents=4000)))
    dinner.add_item("Tonight's fish", 3500, is_special=True)
    st = dinner.state()
    assert st["restaurant"]["name"] == "Test Bistro" and st["restaurant"]["dishes"] == 1

    again = new_dinner(organiser, "  test   BISTRO ")
    assert again.state()["dinner"]["restaurant_name"] == "Test Bistro"
    seen = names(again, guest=True)
    assert set(seen) == {"Steak"}  # published straight away; specials aren't kept
    assert seen["Steak"]["price_cents"] == 4000
    page = again.state()["menu"]["pages"][0]
    assert page["from_saved"] and page["status"] == "published"
    assert organiser.get(f"{again.base}/image/{page['image']}").status_code == 200

    listed = organiser.get("/api/o/restaurants").json()["restaurants"]
    assert [r["name"] for r in listed] == ["Test Bistro"]


def test_starting_fresh_is_allowed(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    blank = new_dinner(organiser, "Test Bistro", use_saved_menu=False)
    assert blank.state()["menu"]["items"] == []


def test_rescan_updates_dishes_in_place_and_lists_the_missing(dinner, organiser):
    scan_and_publish(
        dinner,
        (1, 2, 3),
        menu(
            _item(name="Steak", price_text="$40", price_cents=4000),
            _item(name="Parma", price_text="$28", price_cents=2800),
            _item(name="Old Pie", price_text="$22", price_cents=2200),
        ),
    )
    again = new_dinner(organiser)
    scan_and_publish(
        again,
        (4, 5, 6),
        menu(
            _item(name="STEAK", price_text="$42", price_cents=4200),
            _item(name="Parma", price_text="$28", price_cents=2800),
            _item(name="Burger", price_text="$25", price_cents=2500),
        ),
    )
    items = names(again)
    assert len(items) == 4  # no duplicates: Steak and Parma updated, Burger added, Old Pie left over
    assert items["STEAK"]["price_cents"] == 4200 and items["STEAK"]["change_note"] == "Price was $40.00"
    assert items["Parma"]["change_note"] == "" and not items["Parma"]["from_saved"]
    assert items["Burger"]["change_note"] == "New"
    assert items["Old Pie"]["from_saved"]

    r = organiser.post(f"/api/o/d/{again.id}/menu/drop-unseen", headers=H)
    assert r.json()["removed"] == 1
    assert set(names(again)) == {"STEAK", "Parma", "Burger"}
    assert not any(p["from_saved"] for p in again.state()["menu"]["pages"])  # emptied saved page went too

    third = new_dinner(organiser)
    assert set(names(third)) == {"STEAK", "Parma", "Burger"}
    assert all(i["change_note"] == "" and i["from_saved"] for i in names(third).values())


def test_old_dinner_cannot_roll_back_the_saved_menu(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    newer = new_dinner(organiser)
    newer.add_item("Burger", 2500)
    dinner.add_item("Something old", 1000)
    assert set(names(new_dinner(organiser))) == {"Steak", "Burger"}


def test_old_bills_keep_their_own_prices(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    again = new_dinner(organiser)
    scan_and_publish(again, (4, 5, 6), menu(_item(name="Steak", price_cents=4500)))
    assert names(dinner)["Steak"]["price_cents"] == 4000


def test_demo_dinners_never_touch_restaurants(organiser):
    organiser.post("/api/o/demo", headers=H)
    assert organiser.get("/api/o/restaurants").json()["restaurants"] == []


def test_a_full_rescan_leaves_no_old_pages_behind(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    again = new_dinner(organiser)
    scan_and_publish(again, (4, 5, 6), menu(_item(name="Steak", price_cents=4000)))
    pages = again.state()["menu"]["pages"]
    assert len(pages) == 1 and not pages[0]["from_saved"]
    assert [i["name"] for i in again.state()["menu"]["items"]] == ["Steak"]


def test_restaurants_list_most_recent_first_with_visits(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    new_dinner(organiser, "Lantern Kitchen")
    new_dinner(organiser, "test bistro")
    listed = organiser.get("/api/o/restaurants").json()["restaurants"]
    assert [(r["name"], r["visits"]) for r in listed] == [("Test Bistro", 2), ("Lantern Kitchen", 1)]
    assert listed[0]["dishes"] == 1 and listed[0]["last_visit"] and listed[0]["menu_updated_at"]
    assert listed[1]["dishes"] == 0 and listed[1]["menu_updated_at"] is None


def test_start_a_dinner_by_picking_a_restaurant(dinner, organiser):
    scan_and_publish(dinner, (1, 2, 3), menu(_item(name="Steak", price_cents=4000)))
    rid = organiser.get("/api/o/restaurants").json()["restaurants"][0]["id"]
    r = organiser.post("/api/o/dinners", json={"restaurant_id": rid, "table_label": "7"}, headers=H)
    picked = Dinner(organiser, r.json()["id"])
    st = picked.state()
    assert st["dinner"]["restaurant_name"] == "Test Bistro" and st["dinner"]["table_label"] == "7"
    assert set(names(picked)) == {"Steak"}
    gone = organiser.post("/api/o/dinners", json={"restaurant_id": "nope"}, headers=H)
    assert gone.status_code == 404


# ------------------------------------------------------- restaurant page
def place_id(org, name="Test Bistro") -> str:
    return next(r["id"] for r in org.get("/api/o/restaurants").json()["restaurants"] if r["name"] == name)


def place_scan(org, rid, colour, result: MenuExtraction) -> str:
    image = png(colour)
    CannedExtractor.register_menu(normalise(image), result)
    r = org.post(
        f"/api/o/r/{rid}/pages", files=[("files", ("m.png", image, "image/png"))], data={"kind": "menu"}, headers=H
    )
    assert r.status_code == 200, r.text
    page = r.json()["pages"][0]
    assert org.post(f"/api/o/r/{rid}/pages/{page}/publish", headers=H).status_code == 200
    return page


def place_items(org, rid) -> dict:
    return {i["name"]: i for i in org.get(f"/api/o/r/{rid}/state").json()["menu"]["items"]}


def test_add_a_menu_on_the_restaurant_page_without_a_dinner(organiser):
    new_dinner(organiser, "Akiba")  # been once, never scanned
    rid = place_id(organiser, "Akiba")
    assert organiser.get(f"/api/o/r/{rid}/state").json()["restaurant"]["dishes"] == 0
    page = place_scan(organiser, rid, (9, 9, 9), menu(_item(name="Bao", price_text="$9", price_cents=900)))

    st = organiser.get(f"/api/o/r/{rid}/state").json()
    assert st["restaurant"]["dishes"] == 1 and st["restaurant"]["menu_updated_at"]
    assert st["restaurant"]["visits"] == 1  # editing the menu isn't a visit
    image = st["menu"]["pages"][0]["image"]
    assert organiser.get(f"/api/o/r/{rid}/image/{image}").status_code == 200
    assert [d["restaurant_name"] for d in organiser.get("/api/o/dinners").json()["dinners"]] == ["Akiba"]

    later = new_dinner(organiser, "Akiba")
    assert set(names(later, guest=True)) == {"Bao"}
    assert later.state()["menu"]["pages"][0]["from_saved"]
    assert page != later.state()["menu"]["pages"][0]["id"]  # the dinner has its own copy


def test_rescan_on_the_restaurant_page_updates_in_place(dinner, organiser):
    scan_and_publish(
        dinner,
        (1, 2, 3),
        menu(_item(name="Steak", price_text="$40", price_cents=4000), _item(name="Old Pie", price_cents=2200)),
    )
    rid = place_id(organiser)
    place_scan(
        organiser,
        rid,
        (7, 7, 7),
        menu(_item(name="Steak", price_text="$45", price_cents=4500), _item(name="Burger", price_cents=2500)),
    )
    items = place_items(organiser, rid)
    assert items["Steak"]["price_cents"] == 4500 and items["Steak"]["change_note"] == "Price was $40.00"
    assert items["Burger"]["change_note"] == "New" and items["Old Pie"]["from_saved"]
    assert organiser.post(f"/api/o/r/{rid}/menu/drop-unseen", headers=H).json()["removed"] == 1
    assert set(place_items(organiser, rid)) == {"Steak", "Burger"}

    # The old dinner keeps its own prices, and can't overwrite the newer edit.
    assert names(dinner)["Steak"]["price_cents"] == 4000
    dinner.add_item("Something old", 1000)
    assert set(place_items(organiser, rid)) == {"Steak", "Burger"}
    nxt = new_dinner(organiser)
    assert names(nxt)["Steak"]["price_cents"] == 4500 and names(nxt)["Steak"]["change_note"] == ""


def test_a_later_dinner_still_updates_the_restaurant(organiser):
    new_dinner(organiser, "Akiba")
    rid = place_id(organiser, "Akiba")
    place_scan(organiser, rid, (9, 9, 9), menu(_item(name="Bao", price_cents=900)))
    tonight = new_dinner(organiser, "Akiba")
    tonight.add_item("Ramen", 2200)
    assert set(place_items(organiser, rid)) == {"Bao", "Ramen"}


def test_restaurant_page_edits_and_limits(organiser):
    new_dinner(organiser, "Akiba")
    rid = place_id(organiser, "Akiba")
    item = organiser.post(f"/api/o/r/{rid}/items", json={"name": "Gyoza", "price_cents": 1200}, headers=H).json()["id"]
    assert place_items(organiser, rid)["Gyoza"]["price_cents"] == 1200
    v = place_items(organiser, rid)["Gyoza"]["version"]
    r = organiser.patch(f"/api/o/r/{rid}/items/{item}", json={"version": v, "price_cents": 1300}, headers=H)
    assert r.status_code == 200
    assert place_items(organiser, rid)["Gyoza"]["price_cents"] == 1300
    specials = organiser.post(
        f"/api/o/r/{rid}/pages",
        files=[("files", ("m.png", png((1, 1, 1)), "image/png"))],
        data={"kind": "specials"},
        headers=H,
    )
    assert specials.status_code == 422  # tonight-only belongs to a dinner
    assert organiser.get("/api/o/r/nope/state").status_code == 404
    # A dish from one place can't be edited through another.
    other = new_dinner(organiser, "Elsewhere")
    assert organiser.delete(f"/api/o/d/{other.id}/items/{item}", headers=H).status_code == 404
    assert organiser.get(f"/o/r/{rid}").status_code == 200
