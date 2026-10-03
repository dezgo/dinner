"""Restaurants remember their menu: saving, starting from it, rescanning."""

from app.services.extraction import ExtractedCategory, MenuExtraction
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
