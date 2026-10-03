"""Menu extraction checks, review/publish visibility and manual correction."""

from app.services.demo import MENU_1, menu_page_1
from app.services.extraction import CannedExtractor, ExtractedItem, Flag, LegendEntry, MenuExtraction
from app.services.images import normalise
from tests.conftest import H, new_client


def png(colour) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), colour).save(buf, "PNG")
    return buf.getvalue()


def _item(**kw):
    base = dict(
        name="Dish",
        description="",
        price_text="",
        price_cents=None,
        variants=[],
        extras=[],
        dietary_labels=[],
        explicit_diet=[],
        vegan_on_request="",
        possibly_vegan=False,
        possibly_vegan_reason="",
        flags=[],
    )
    base.update(kw)
    return ExtractedItem(**base)


def upload_page(dinner, image: bytes, result: MenuExtraction):
    CannedExtractor.register_menu(normalise(image), result)
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages",
        files=[("files", ("m.png", image, "image/png"))],
        data={"kind": "menu"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    return r.json()["pages"][0]


def test_variants_extras_and_flags_survive_extraction(dinner):
    page = upload_page(dinner, menu_page_1(), MENU_1)
    st = dinner.state()
    assert next(p for p in st["menu"]["pages"] if p["id"] == page)["status"] == "review"
    items = {i["name"]: i for i in st["menu"]["items"]}
    tikka = items["Chicken Tikka"]
    assert [v["label"] for v in tikka["variants"]] == ["Half", "Full"]
    assert tikka["price_cents"] is None and not any(f["field"] == "price" for f in tikka["flags"])
    assert items["Lamb Rogan Josh"]["extras"][0]["label"] == "Add extra lamb"
    curries = next(c for c in st["menu"]["categories"] if c["name"] == "Curries")
    assert [e["label"] for e in curries["extras"]] == ["Steamed rice", "Extra naan"]
    # Uncertain and missing prices are flagged, never invented.
    assert items["Papadums"]["flags"]
    assert items["Prawn Malabar"]["price_cents"] is None and items["Prawn Malabar"]["flags"]


def test_vegan_distinctions(dinner):
    upload_page(dinner, menu_page_1(), MENU_1)
    items = {i["name"]: i for i in dinner.state()["menu"]["items"]}
    assert items["Vegetable Samosas (2)"]["vegan"]["status"] == "marked"
    assert items["Dal Tadka"]["vegan"] == {"status": "on_request", "note": "Vegan without ghee"}
    assert items["Tofu & Spinach Curry"]["vegan"]["status"] == "possible"
    # Vegetarian is not vegan.
    assert items["Paneer Makhani"]["vegan"].get("status") is None
    assert "vegan" not in items["Paneer Makhani"]["diet"]


def test_unsupported_vegan_claim_is_downgraded_and_flagged(dinner):
    # The model says "vegan" but nothing printed supports it (only a V = vegetarian).
    result = MenuExtraction(
        legend=[LegendEntry(symbol="V", meaning="vegetarian")],
        page_notes=[],
        categories=[
            {
                "name": "Mains",
                "note": "",
                "extras": [],
                "items": [
                    _item(
                        name="Veg Lasagne",
                        description="ricotta, spinach",
                        price_text="22",
                        price_cents=2200,
                        dietary_labels=["V"],
                        explicit_diet=["vegetarian", "vegan"],
                    ),
                ],
            }
        ],
    )
    upload_page(dinner, png((1, 2, 3)), result)
    item = dinner.state()["menu"]["items"][0]
    assert item["vegan"]["status"] == "possible"
    assert "vegan" not in item["diet"]
    assert any(f["field"] == "dietary" for f in item["flags"])


def test_price_text_mismatch_is_flagged(dinner):
    result = MenuExtraction(
        legend=[],
        page_notes=[],
        categories=[
            {
                "name": "Menu",
                "note": "",
                "extras": [],
                "items": [
                    _item(name="Soup", price_text="14.50", price_cents=1450),
                    _item(
                        name="Bread",
                        price_text="8",
                        price_cents=800 + 100,
                        flags=[Flag(field="name", message="smudged")],
                    ),
                ],
            }
        ],
    )
    upload_page(dinner, png((9, 9, 9)), result)
    items = {i["name"]: i for i in dinner.state()["menu"]["items"]}
    assert items["Soup"]["flags"] == []
    assert any("printed '8'" in f["message"] for f in items["Bread"]["flags"])


def test_guests_only_see_published_pages_and_manual_items(dinner):
    page = upload_page(dinner, menu_page_1(), MENU_1)
    helen = dinner.guest("Helen")
    dinner.add_item("Daily special: Barramundi", 3400, is_special=True)
    names = [i["name"] for i in dinner.gstate(helen)["menu"]["items"]]
    assert names == ["Daily special: Barramundi"]
    assert dinner.gstate(helen)["menu"]["pending_pages"] == 1
    assert dinner.org.post(f"/api/o/d/{dinner.id}/pages/{page}/publish", headers=H).status_code == 200
    names = [i["name"] for i in dinner.gstate(helen)["menu"]["items"]]
    assert "Garlic Naan" in names and len(names) == 10


def test_failed_extraction_falls_back_to_manual(dinner):
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages",
        files=[("files", ("x.png", png((200, 0, 0)), "image/png"))],
        data={"kind": "menu"},
        headers=H,
    )
    page = dinner.state()["menu"]["pages"][0]
    assert r.status_code == 200 and page["status"] == "failed"
    assert "manually" in page["error"]


def test_non_images_are_rejected(dinner):
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages", files=[("files", ("x.jpg", b"not an image", "image/jpeg"))], headers=H
    )
    assert r.status_code == 422


def pdf(pages: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    imgs = [Image.new("RGB", (300, 420), (255, 255, 255)) for _ in range(pages)]
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def test_pdf_menu_becomes_one_page_each(dinner):
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages",
        files=[("files", ("menu.pdf", pdf(3), "application/pdf"))],
        data={"kind": "menu"},
        headers=H,
    )
    assert r.status_code == 200 and len(r.json()["pages"]) == 3
    pages = dinner.state()["menu"]["pages"]
    assert len(pages) == 3 and all(p["image"].endswith(".jpg") for p in pages)


def test_too_many_pdf_pages_is_a_clear_refusal(dinner):
    r = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages", files=[("files", ("menu.pdf", pdf(13), "application/pdf"))], headers=H
    )
    assert r.status_code == 422 and "13 pages" in r.json()["detail"]
    broken = dinner.org.post(
        f"/api/o/d/{dinner.id}/pages", files=[("files", ("x.pdf", b"%PDF-1.4 junk", "application/pdf"))], headers=H
    )
    assert broken.status_code == 422


def test_organiser_corrects_item_with_version_check(dinner):
    item = dinner.add_item("Pavlova", None)
    v = next(i for i in dinner.state()["menu"]["items"] if i["id"] == item)["version"]
    ok = dinner.org.patch(f"/api/o/d/{dinner.id}/items/{item}", json={"version": v, "price_cents": 1600}, headers=H)
    assert ok.status_code == 200
    stale = dinner.org.patch(f"/api/o/d/{dinner.id}/items/{item}", json={"version": v, "price_cents": 1}, headers=H)
    assert stale.status_code == 409


def test_unavailable_dish_cannot_be_recorded_by_guest(dinner):
    item = dinner.add_item("Oysters", 2800)
    v = dinner.state()["menu"]["items"][0]["version"]
    dinner.org.patch(f"/api/o/d/{dinner.id}/items/{item}", json={"version": v, "unavailable": True}, headers=H)
    helen = dinner.guest("Helen")
    r = helen.post(f"{dinner.base}/lines", json={"menu_item_id": item}, headers=H)
    assert r.status_code == 409


def test_guest_cannot_see_unpublished_photo(dinner):
    page = upload_page(dinner, menu_page_1(), MENU_1)
    helen = dinner.guest("Helen")
    image = dinner.state()["menu"]["pages"][0]["image"]
    assert helen.get(f"{dinner.base}/image/{image}").status_code == 404
    dinner.org.post(f"/api/o/d/{dinner.id}/pages/{page}/publish", headers=H)
    assert helen.get(f"{dinner.base}/image/{image}").status_code == 200
    stranger = new_client()
    assert stranger.get(f"{dinner.base}/image/{image}").status_code == 401
