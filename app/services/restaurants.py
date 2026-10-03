"""Restaurants remember their menu between visits.

Whenever the organiser changes a dinner's menu, the published part of it
(minus tonight-only specials and "unavailable tonight" marks) is saved on the
restaurant, with its photos. A new dinner at the same place starts with a copy
of that saved menu, ready for guests. Scanning the menu again on a later visit
updates the copied dishes in place and notes what changed (see
`match_saved_item`, used by menu.apply_extraction); dishes the new scan didn't
find are listed for the organiser to remove.

Each dinner keeps its own copy, so updating a restaurant's menu never changes
an old bill.
"""

from __future__ import annotations

import re
import shutil

from sqlmodel import Session, col, select

from app.models import Dinner, MenuCategory, MenuItem, MenuPage, Restaurant, utcnow
from app.services.images import dinner_dir, image_path
from app.services.money import fmt

ITEM_COPY = (
    "name",
    "description",
    "price_cents",
    "price_text",
    "variants",
    "extras",
    "labels",
    "diet",
    "vegan",
    "flags",
    "source",
    "sort",
)


def name_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def dish_key(name: str) -> str:
    """Loose match for dish names across scans: case, spacing and punctuation ignored."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _folder(restaurant: Restaurant) -> str:
    return f"r{restaurant.id}"


def find_or_create(s: Session, name: str) -> Restaurant | None:
    key = name_key(name)
    if not key:
        return None
    r = s.exec(select(Restaurant).where(Restaurant.name_key == key)).first()
    if r is None:
        r = Restaurant(name=" ".join(name.split())[:80], name_key=key)
        s.add(r)
        s.flush()
    return r


def link(s: Session, dinner: Dinner) -> Restaurant | None:
    """Point the dinner at the restaurant its name refers to (demo dinners never are)."""
    r = None if dinner.is_demo else find_or_create(s, dinner.restaurant_name)
    dinner.restaurant_id = r.id if r else None
    if r is not None:
        dinner.restaurant_name = r.name  # "lantern kitchen" becomes the name as first entered
    s.add(dinner)
    return r


def summary(s: Session, dinner: Dinner) -> dict | None:
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    if r is None:
        return None
    return {
        "id": r.id,
        "name": r.name,
        "menu_updated_at": r.menu_updated_at.isoformat() if r.menu_updated_at else None,
        "dishes": len(r.menu.get("items", [])) if r.menu else 0,
    }


def listing(s: Session) -> list[dict]:
    rows = s.exec(select(Restaurant).order_by(col(Restaurant.name))).all()
    return [
        {
            "name": r.name,
            "menu_updated_at": r.menu_updated_at.isoformat() if r.menu_updated_at else None,
            "dishes": len(r.menu.get("items", [])) if r.menu else 0,
        }
        for r in rows
    ]


# ------------------------------------------------------------------- saving
def save_menu(s: Session, dinner: Dinner) -> None:
    """Save the dinner's published menu on its restaurant.

    Only the restaurant's newest dinner writes, so tidying an old dinner later
    can't roll the saved menu back. A dinner with nothing published leaves the
    saved menu alone.
    """
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    if r is None or dinner.is_demo:
        return
    newest = s.exec(
        select(Dinner.id).where(Dinner.restaurant_id == r.id).order_by(col(Dinner.created_at).desc())
    ).first()
    if newest != dinner.id:
        return
    pages = s.exec(
        select(MenuPage)
        .where(MenuPage.dinner_id == dinner.id, MenuPage.status == "published", MenuPage.kind != "specials")
        .order_by(col(MenuPage.sort))
    ).all()
    page_ids = {p.id for p in pages}
    items = [
        i
        for i in s.exec(select(MenuItem).where(MenuItem.dinner_id == dinner.id).order_by(col(MenuItem.sort))).all()
        if not i.is_special and (i.page_id in page_ids or (i.page_id is None and not i.unavailable))
    ]
    if not items:
        return
    cat_ids = {i.category_id for i in items if i.category_id}
    cats = [
        c
        for c in s.exec(
            select(MenuCategory).where(MenuCategory.dinner_id == dinner.id).order_by(col(MenuCategory.sort))
        ).all()
        if c.id in cat_ids
    ]
    folder = dinner_dir(_folder(r))
    for p in pages:
        src = image_path(dinner.id, p.image_file) if p.image_file else None
        if src is not None and not (folder / p.image_file).exists():
            shutil.copyfile(src, folder / p.image_file)
    r.menu = {
        "pages": [{"key": p.id, "image": p.image_file, "legend": p.legend, "flags": p.flags} for p in pages],
        "categories": [{"key": c.id, "name": c.name, "note": c.note, "extras": c.extras} for c in cats],
        "items": [
            {**{f: getattr(i, f) for f in ITEM_COPY}, "page": i.page_id, "category": i.category_id} for i in items
        ],
    }
    r.menu_dinner_id = dinner.id
    r.menu_updated_at = utcnow()
    s.add(r)


# ------------------------------------------------------------------ loading
def load_menu(s: Session, dinner: Dinner) -> int:
    """Give a new dinner a copy of its restaurant's saved menu. Returns the dish count."""
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    if r is None or not r.menu or not r.menu.get("items"):
        return 0
    folder = dinner_dir(_folder(r))
    target = dinner_dir(dinner.id)
    pages: dict[str, str] = {}
    for n, p in enumerate(r.menu.get("pages", [])):
        image = p.get("image")
        if image and (folder / image).is_file():
            shutil.copyfile(folder / image, target / image)
        else:
            image = None
        page = MenuPage(
            dinner_id=dinner.id,
            kind="menu",
            image_file=image,
            status="published",
            legend=p.get("legend") or [],
            flags=p.get("flags") or [],
            from_saved=True,
            sort=n,
        )
        s.add(page)
        pages[p["key"]] = page.id
    cats: dict[str, str] = {}
    for n, c in enumerate(r.menu.get("categories", [])):
        cat = MenuCategory(
            dinner_id=dinner.id, name=c["name"], note=c.get("note", ""), extras=c.get("extras") or [], sort=n
        )
        s.add(cat)
        cats[c["key"]] = cat.id
    for x in r.menu["items"]:
        s.add(
            MenuItem(
                dinner_id=dinner.id,
                page_id=pages.get(x.get("page")),
                category_id=cats.get(x.get("category")),
                from_saved=True,
                **{f: x[f] for f in ITEM_COPY if f in x},
            )
        )
    return len(r.menu["items"])


# ----------------------------------------------------------------- rescans
def saved_items(s: Session, dinner_id: str) -> dict[str, MenuItem]:
    """Copied dishes a fresh scan hasn't found yet, by loose name."""
    rows = s.exec(select(MenuItem).where(MenuItem.dinner_id == dinner_id, MenuItem.from_saved == True)).all()  # noqa: E712
    return {dish_key(i.name): i for i in rows}


def describe_change(old: MenuItem, new: dict) -> str:
    """A short note on what a fresh scan changed, or "" if nothing a diner would care about."""
    notes = []
    if old.price_cents != new["price_cents"]:
        notes.append(f"Price was {fmt(old.price_cents) if old.price_cents is not None else 'not shown'}")
    old_v = {v["label"]: v.get("price_cents") for v in old.variants or []}
    new_v = {v["label"]: v.get("price_cents") for v in new["variants"]}
    if old_v != new_v:
        was = ", ".join(f"{k} {fmt(v) if v is not None else '?'}" for k, v in old_v.items())
        notes.append(f"Sizes or their prices changed (was {was})" if old_v else "Now comes in sizes")
    if " ".join(old.description.split()) != " ".join(new["description"].split()):
        notes.append("Description changed")
    return "; ".join(notes)


def link_existing() -> None:
    """One-off on start-up: dinners from before restaurants existed get linked,
    and each restaurant's newest dinner provides its saved menu."""
    from app.db import locked_write

    with locked_write() as s:
        loose = s.exec(
            select(Dinner)
            .where(Dinner.restaurant_id == None, Dinner.is_demo == False, Dinner.restaurant_name != "")  # noqa: E711, E712
            .order_by(col(Dinner.created_at))
        ).all()
        for d in loose:
            link(s, d)
        s.flush()
        for d in loose:
            save_menu(s, d)
