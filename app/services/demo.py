"""A demo dinner to try every flow without an API key or real money.

"Lantern Kitchen" has two menu pages and an itemised receipt, drawn as images
at start-up. Their extraction results are pre-registered, so they go through
the real upload -> processing -> review pipeline without calling Claude. The
data deliberately includes the awkward cases: a vegetarian dish that is not
vegan, a "vegan on request" dish, an AI-only "possibly vegan", a missing
price, an unclear price, variants and extras, duplicate names, a shared dish,
per-unit drinks, an abbreviated receipt, a changed price, an item missing
from the receipt, one not recorded, a surcharge, and GST already included.

Demo dinners never match real bank transfers; use "Simulate a payment".
"""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from app.db import locked_write
from app.models import MenuPage, Receipt
from app.services import dinners, menu, orders, reconcile
from app.services.extraction import (
    CannedExtractor,
    ExtractedCategory,
    ExtractedItem,
    ExtractedReceiptLine,
    Flag,
    LegendEntry,
    MenuExtraction,
    PriceOption,
    ReceiptExtraction,
)
from app.services.images import normalise, store_image
from app.services.state import payment_profile


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _draw(lines: list[tuple[str, int, str]], size=(900, 1300), bg=(250, 246, 236)) -> bytes:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    y = 40
    for text, fsize, align in lines:
        f = _font(fsize)
        if align == "center":
            w = d.textlength(text, font=f)
            d.text(((size[0] - w) / 2, y), text, fill=(40, 30, 20), font=f)
        elif "\t" in text:
            left, right = text.split("\t")
            d.text((60, y), left, fill=(40, 30, 20), font=f)
            w = d.textlength(right, font=f)
            d.text((size[0] - 60 - w, y), right, fill=(40, 30, 20), font=f)
        else:
            d.text((60, y), text, fill=(90, 80, 70), font=f)
        y += int(fsize * 1.45)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@lru_cache
def menu_page_1() -> bytes:
    return _draw(
        [
            ("LANTERN KITCHEN", 44, "center"),
            ("", 14, "left"),
            ("STARTERS", 30, "left"),
            ("Garlic Naan  V\t6.00", 24, "l"),
            ("wood-fired, garlic butter", 20, "left"),
            ("Vegetable Samosas (2)  V VG\t9.50", 24, "l"),
            ("potato, peas, tamarind chutney", 20, "left"),
            ("Chicken Tikka  GF\thalf 14 / full 24", 24, "l"),
            ("Papadums\t4", 24, "l"),
            ("", 14, "left"),
            ("CURRIES", 30, "left"),
            ("all curries: add steamed rice 4 / extra naan 5", 20, "left"),
            ("Lamb Rogan Josh  GF\t28.00", 24, "l"),
            ("slow-cooked lamb, Kashmiri chilli", 20, "left"),
            ("   add extra lamb +8", 20, "left"),
            ("Paneer Makhani  V GF\t24.00", 24, "l"),
            ("cottage cheese, tomato, butter, cream", 20, "left"),
            ("Tofu & Spinach Curry\t23.00", 24, "l"),
            ("tofu, spinach, onion, cumin", 20, "left"),
            ("Dal Tadka  V\t19.00", 24, "l"),
            ("yellow lentils, garlic, cumin. Vegan without ghee", 20, "left"),
            ("Prawn Malabar\tMP", 24, "l"),
            ("prawns, coconut, curry leaf", 20, "left"),
            ("", 14, "left"),
            ("V vegetarian   VG vegan   GF gluten free", 20, "left"),
        ]
    )


@lru_cache
def menu_page_2() -> bytes:
    return _draw(
        [
            ("DRINKS & SPECIALS", 40, "center"),
            ("", 14, "left"),
            ("BEER", 30, "left"),
            ("Kingfisher\tglass 9 / pint 12", 24, "l"),
            ("WINE", 30, "left"),
            ("House Red\tglass 11 / bottle 48", 24, "l"),
            ("SOFT", 30, "left"),
            ("Mango Lassi  V\t7.50", 24, "l"),
            ("", 14, "left"),
            ("TONIGHT'S SPECIAL", 30, "left"),
            ("Goat Curry\t30", 24, "l"),
            ("bone-in goat, [glare] ... masala", 20, "left"),
        ],
        size=(900, 900),
    )


@lru_cache
def receipt_image() -> bytes:
    rows = [
        ("LANTERN KITCHEN", 34, "center"),
        ("TAX INVOICE  ABN 00 000 000 000", 18, "center"),
        ("", 12, "left"),
        ("2 GRLC NAAN\t12.00", 22, "l"),
        ("1 VEG SAMOSA\t9.50", 22, "l"),
        ("1 LAMB RGN JSH\t28.00", 22, "l"),
        ("1 PNR MAKHANI\t24.00", 22, "l"),
        ("1 TOFU SPIN CRY\t23.00", 22, "l"),
        ("1 DAL TADKA\t19.00", 22, "l"),
        ("3 KFSHR PINT\t36.00", 22, "l"),
        ("1 MANGO LASSI\t7.50", 22, "l"),
        ("1 HSE RED GLS\t12.00", 22, "l"),
        ("", 12, "left"),
        ("SUBTOTAL\t171.00", 22, "l"),
        ("SUNDAY SURCHARGE 10%\t17.10", 22, "l"),
        ("TOTAL\t188.10", 28, "l"),
        ("GST INCLUDED\t17.10", 20, "l"),
        ("VISA\t188.10", 20, "l"),
    ]
    return _draw(rows, size=(700, 1000), bg=(255, 255, 255))


def _o(label: str, text: str, cents: int | None) -> PriceOption:
    return PriceOption(label=label, price_text=text, price_cents=cents)


def _i(
    name,
    desc,
    text,
    cents,
    labels=(),
    diet=(),
    variants=(),
    extras=(),
    on_request="",
    possibly=False,
    reason="",
    flags=(),
) -> ExtractedItem:
    return ExtractedItem(
        name=name,
        description=desc,
        price_text=text,
        price_cents=cents,
        variants=list(variants),
        extras=list(extras),
        dietary_labels=list(labels),
        explicit_diet=list(diet),
        vegan_on_request=on_request,
        possibly_vegan=possibly,
        possibly_vegan_reason=reason,
        flags=list(flags),
    )


MENU_1 = MenuExtraction(
    legend=[
        LegendEntry(symbol="V", meaning="vegetarian"),
        LegendEntry(symbol="VG", meaning="vegan"),
        LegendEntry(symbol="GF", meaning="gluten free"),
    ],
    page_notes=[],
    categories=[
        ExtractedCategory(
            name="Starters",
            note="",
            extras=[],
            items=[
                _i("Garlic Naan", "wood-fired, garlic butter", "6.00", 600, ["V"], ["vegetarian"]),
                _i(
                    "Vegetable Samosas (2)",
                    "potato, peas, tamarind chutney",
                    "9.50",
                    950,
                    ["V", "VG"],
                    ["vegetarian", "vegan"],
                ),
                _i(
                    "Chicken Tikka",
                    "",
                    "",
                    None,
                    ["GF"],
                    ["gluten_free"],
                    variants=[_o("Half", "half 14", 1400), _o("Full", "full 24", 2400)],
                ),
                _i(
                    "Papadums",
                    "",
                    "4",
                    400,
                    flags=[Flag(field="price", message="The '4' may belong to the line above.")],
                ),
            ],
        ),
        ExtractedCategory(
            name="Curries",
            note="All curries: add steamed rice or extra naan.",
            extras=[_o("Steamed rice", "4", 400), _o("Extra naan", "5", 500)],
            items=[
                _i(
                    "Lamb Rogan Josh",
                    "slow-cooked lamb, Kashmiri chilli",
                    "28.00",
                    2800,
                    ["GF"],
                    ["gluten_free"],
                    extras=[_o("Add extra lamb", "+8", 800)],
                ),
                _i(
                    "Paneer Makhani",
                    "cottage cheese, tomato, butter, cream",
                    "24.00",
                    2400,
                    ["V", "GF"],
                    ["vegetarian", "gluten_free"],
                ),
                _i(
                    "Tofu & Spinach Curry",
                    "tofu, spinach, onion, cumin",
                    "23.00",
                    2300,
                    possibly=True,
                    reason="No animal products are listed in the description.",
                ),
                _i(
                    "Dal Tadka",
                    "yellow lentils, garlic, cumin. Vegan without ghee",
                    "19.00",
                    1900,
                    ["V"],
                    ["vegetarian"],
                    on_request="Vegan without ghee",
                ),
                _i(
                    "Prawn Malabar",
                    "prawns, coconut, curry leaf",
                    "MP",
                    None,
                    flags=[Flag(field="price", message="Market price — ask staff.")],
                ),
            ],
        ),
    ],
)

MENU_2 = MenuExtraction(
    legend=[LegendEntry(symbol="V", meaning="vegetarian")],
    page_notes=["Glare over part of the specials section."],
    categories=[
        ExtractedCategory(
            name="Drinks",
            note="",
            extras=[],
            items=[
                _i("Kingfisher", "", "", None, variants=[_o("Glass", "glass 9", 900), _o("Pint", "pint 12", 1200)]),
                _i(
                    "House Red", "", "", None, variants=[_o("Glass", "glass 11", 1100), _o("Bottle", "bottle 48", 4800)]
                ),
                _i("Mango Lassi", "", "7.50", 750, ["V"], ["vegetarian"]),
            ],
        ),
        ExtractedCategory(
            name="Tonight's special",
            note="",
            extras=[],
            items=[
                _i(
                    "Goat Curry",
                    "bone-in goat, … masala",
                    "30",
                    3000,
                    flags=[Flag(field="description", message="Part of the description is hidden by glare.")],
                ),
            ],
        ),
    ],
)


def _r(desc, qty, unit, total, kind="item") -> ExtractedReceiptLine:
    return ExtractedReceiptLine(
        description=desc,
        quantity=qty,
        unit_price_cents=unit,
        line_total_cents=total,
        kind=kind,
        uncertain=False,
        note="",
    )


RECEIPT = ReceiptExtraction(
    lines=[
        _r("2 GRLC NAAN", 2, 600, 1200),
        _r("1 VEG SAMOSA", 1, 950, 950),
        _r("1 LAMB RGN JSH", 1, 2800, 2800),
        _r("1 PNR MAKHANI", 1, 2400, 2400),
        _r("1 TOFU SPIN CRY", 1, 2300, 2300),
        _r("1 DAL TADKA", 1, 1900, 1900),
        _r("3 KFSHR PINT", 3, 1200, 3600),
        _r("1 MANGO LASSI", 1, 750, 750),
        _r("1 HSE RED GLS", 1, 1200, 1200),
        _r("SUBTOTAL", 1, None, 17100, "subtotal"),
        _r("SUNDAY SURCHARGE 10%", 1, None, 1710, "surcharge"),
        _r("TOTAL", 1, None, 18810, "total"),
        _r("GST INCLUDED", 1, None, 1710, "tax_info"),
        _r("VISA", 1, None, 18810, "payment"),
    ],
    subtotal_cents=17100,
    total_cents=18810,
    gst_cents=1710,
    gst_included=True,
    notes=[],
)


def register_samples() -> None:
    CannedExtractor.register_menu(normalise(menu_page_1()), MENU_1)
    CannedExtractor.register_menu(normalise(menu_page_2()), MENU_2)
    CannedExtractor.register_receipt(normalise(receipt_image()), RECEIPT)


def create_demo_dinner() -> str:
    register_samples()
    with locked_write() as s:
        me = payment_profile(s).get("my_name") or "Derek"
        d = dinners.create_dinner(s, "Demo — Lantern Kitchen", me, is_demo=True)
        dinner_id = d.id
    p1 = MenuPage(dinner_id=dinner_id, image_file=store_image(dinner_id, menu_page_1()), sort=0)
    p2 = MenuPage(dinner_id=dinner_id, image_file=store_image(dinner_id, menu_page_2()), sort=1)
    with locked_write() as s:
        s.add(p1)
        s.add(p2)
    menu.apply_extraction(p1.id, MENU_1)
    menu.apply_extraction(p2.id, MENU_2)

    from sqlmodel import select

    from app.models import Dinner, MenuItem, Participant

    with locked_write() as s:
        d = s.get(Dinner, dinner_id)
        for p in s.exec(select(MenuPage).where(MenuPage.dinner_id == dinner_id)).all():
            p.status = "published"
            s.add(p)
        items = {i.name: i for i in s.exec(select(MenuItem).where(MenuItem.dinner_id == dinner_id)).all()}
        me = s.get(Participant, d.organiser_participant_id)
        helen = dinners.add_participant(s, d, "Helen")
        helen2 = dinners.add_participant(s, d, "Helen")
        heleng = dinners.add_participant(s, d, "Helen G")
        tom = dinners.add_participant(s, d, "Tom")
        priya = dinners.add_participant(s, d, "Priya")

        def rec(who, name, **kw):
            data = {"menu_item_id": items[name].id, **kw}
            return orders.record_line(s, d, orders.Actor(who, False), data)

        rec(helen, "Lamb Rogan Josh")
        rec(helen, "House Red", variant_label="Glass")
        rec(helen2, "Tofu & Spinach Curry")
        rec(heleng, "Paneer Makhani")
        rec(tom, "Vegetable Samosas (2)")
        rec(priya, "Dal Tadka", note="Vegan please — no ghee")
        rec(me, "Chicken Tikka", variant_label="Half")
        rec(helen, "Garlic Naan", quantity=2, shared=True, participants=[helen.id, tom.id, priya.id])
        pints = rec(
            tom,
            "Kingfisher",
            variant_label="Pint",
            quantity=3,
            shared=True,
            split_mode="units",
            participants=[tom.id],
            weights={tom.id: 2},
        )
        line_id = pints["line"]["id"]
        orders.join_line(s, d, orders.Actor(me, False), line_id, pints["line"]["version"], units=1)
    return dinner_id


def scan_sample_receipt(dinner_id: str) -> str:
    register_samples()
    name = store_image(dinner_id, receipt_image())
    with locked_write() as s:
        r = Receipt(dinner_id=dinner_id, image_files=[name])
        s.add(r)
    reconcile.queue_receipt(r.id)
    return r.id
