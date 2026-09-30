"""The digital menu: pages, extraction results, review and corrections.

A page moves processing -> review -> published. Guests see dishes from
published pages plus anything the organiser typed in by hand, so reviewed
pages can be browsed while others are still being read.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.db import locked_write, session_scope
from app.models import Dinner, MenuCategory, MenuItem, MenuPage
from app.services import jobs
from app.services.events import audit, touch
from app.services.extraction import (
    ExtractionError,
    MenuExtraction,
    check_price,
    get_extractor,
    item_vegan_status,
)
from app.services.images import read_image

logger = logging.getLogger(__name__)

DIET_TAGS = {"vegetarian", "vegan", "gluten_free", "dairy_free", "nut_free", "contains_nuts", "halal", "spicy"}


def queue_page(page_id: str) -> None:
    jobs.submit(process_page, page_id)


def process_page(page_id: str) -> None:
    with session_scope() as s:
        page = s.get(MenuPage, page_id)
        if page is None or page.status != "processing":
            return
        dinner = s.get(Dinner, page.dinner_id)
        image = read_image(page.dinner_id, page.image_file)
        demo = bool(dinner and dinner.is_demo)
    try:
        result = get_extractor(demo=demo).menu(image, "image/jpeg")
    except ExtractionError as e:
        _fail(page_id, str(e))
        return
    except Exception:
        logger.exception("menu extraction crashed")
        _fail(page_id, "Something went wrong reading this page. Retry, or enter items manually.")
        return
    apply_extraction(page_id, result)


def _fail(page_id: str, message: str) -> None:
    with locked_write() as s:
        page = s.get(MenuPage, page_id)
        if page is None:
            return
        page.status = "failed"
        page.error = message
        s.add(page)
        touch(s, s.get(Dinner, page.dinner_id))


def _category(s: Session, dinner_id: str, page_id: str, name: str, cache: dict) -> MenuCategory:
    key = name.strip().lower() or "menu"
    if key in cache:
        return cache[key]
    existing = s.exec(select(MenuCategory).where(MenuCategory.dinner_id == dinner_id)).all()
    for c in existing:
        if c.name.strip().lower() == key:
            cache[key] = c
            return c
    count = len(existing)
    cat = MenuCategory(dinner_id=dinner_id, page_id=page_id, name=name.strip() or "Menu", sort=count)
    s.add(cat)
    cache[key] = cat
    return cat


def _options(opts) -> list[dict]:
    return [
        {"label": o.label.strip(), "price_cents": o.price_cents, "price_text": o.price_text}
        for o in opts
        if o.label.strip()
    ]


def apply_extraction(page_id: str, result: MenuExtraction) -> None:
    with locked_write() as s:
        page = s.get(MenuPage, page_id)
        if page is None or page.status != "processing":
            return
        dinner = s.get(Dinner, page.dinner_id)
        legend = {e.symbol.strip().upper(): e.meaning for e in result.legend}
        page.legend = [{"symbol": e.symbol, "meaning": e.meaning} for e in result.legend]
        page.flags = list(result.page_notes)
        cache: dict = {}
        order = _next_item_sort(s, dinner.id)
        for cat_x in result.categories:
            cat = _category(s, dinner.id, page.id, cat_x.name, cache)
            if cat_x.note and not cat.note:
                cat.note = cat_x.note
            if cat_x.extras:
                cat.extras = (cat.extras or []) + _options(cat_x.extras)
            s.add(cat)
            for x in cat_x.items:
                flags = [f.model_dump() for f in x.flags]
                flags += check_price(x.price_text, x.price_cents, "Price")
                for v in x.variants:
                    flags += check_price(v.price_text, v.price_cents, v.label)
                if x.price_cents is None and not any(v.price_cents is not None for v in x.variants):
                    if not any(f["field"] == "price" for f in flags):
                        flags.append({"field": "price", "message": "No price shown on the menu."})
                vegan, vegan_flags = item_vegan_status(x, legend)
                flags += vegan_flags
                diet = [t for t in x.explicit_diet if t in DIET_TAGS]
                if vegan["status"] != "marked" and "vegan" in diet:
                    diet.remove("vegan")
                order += 1
                s.add(
                    MenuItem(
                        dinner_id=dinner.id,
                        page_id=page.id,
                        category_id=cat.id,
                        name=x.name.strip(),
                        description=x.description.strip(),
                        price_cents=x.price_cents,
                        price_text=x.price_text,
                        variants=_options(x.variants),
                        extras=_options(x.extras),
                        labels=[lbl.strip() for lbl in x.dietary_labels if lbl.strip()],
                        diet=diet,
                        vegan=vegan,
                        flags=flags,
                        is_special=page.kind == "specials",
                        sort=order,
                    )
                )
        page.status = "review"
        page.error = None
        s.add(page)
        audit(s, dinner.id, "system", f"Read menu page ({sum(len(c.items) for c in result.categories)} dishes)")
        touch(s, dinner, "menu")


def _next_item_sort(s: Session, dinner_id: str) -> int:
    last = s.exec(
        select(MenuItem.sort).where(MenuItem.dinner_id == dinner_id).order_by(col(MenuItem.sort).desc())
    ).first()
    return last or 0


def visible_page_ids(s: Session, dinner_id: str) -> set[str]:
    return set(s.exec(select(MenuPage.id).where(MenuPage.dinner_id == dinner_id, MenuPage.status == "published")).all())


def menu_items(s: Session, dinner_id: str, *, include_unpublished: bool) -> list[MenuItem]:
    items = s.exec(select(MenuItem).where(MenuItem.dinner_id == dinner_id).order_by(col(MenuItem.sort))).all()
    if include_unpublished:
        return list(items)
    published = visible_page_ids(s, dinner_id)
    return [i for i in items if i.page_id is None or i.page_id in published]


def check_version(obj, expected: int | None) -> None:
    if expected is not None and obj.version != expected:
        raise HTTPException(409, "Someone else changed this at the same time. It has been refreshed — try again.")


def clean_options(raw) -> list[dict]:
    out = []
    for o in raw or []:
        label = str(o.get("label", "")).strip()[:80]
        if not label:
            continue
        price = o.get("price_cents")
        out.append({"label": label, "price_cents": int(price) if price is not None else None})
    return out


ITEM_FIELDS = {
    "name",
    "description",
    "price_cents",
    "price_text",
    "variants",
    "extras",
    "labels",
    "diet",
    "vegan",
    "unavailable",
    "is_special",
    "category_id",
    "flags",
}


def update_item(s: Session, item: MenuItem, data: dict) -> None:
    for key, value in data.items():
        if key not in ITEM_FIELDS:
            continue
        if key in ("variants", "extras"):
            value = clean_options(value)
        elif key == "diet":
            value = [t for t in value or [] if t in DIET_TAGS]
        elif key == "vegan":
            status = (value or {}).get("status")
            if status not in ("marked", "on_request", "possible", None):
                raise HTTPException(422, "Unknown vegan status.")
            value = {"status": status, "note": str((value or {}).get("note", ""))[:200]}
        elif key == "price_cents" and value is not None:
            value = int(value)
            if value < 0:
                raise HTTPException(422, "Prices can't be negative.")
        elif key in ("name", "description", "price_text"):
            value = str(value or "").strip()[:2000]
            if key == "name" and not value:
                raise HTTPException(422, "A dish needs a name.")
        setattr(item, key, value)
    item.version += 1
    s.add(item)
