"""Restaurants remember their menu between visits.

A restaurant owns a saved menu: ordinary menu pages, sections and dishes whose
`restaurant_id` is set instead of a `dinner_id`. It can be edited on the
restaurant's own page (photograph a new menu ahead of a visit), and it is also
kept current by the latest dinner there: whenever that dinner's menu changes,
its published part (minus tonight-only specials and "unavailable tonight"
marks) replaces the saved one, photos included.

A new dinner at the same place starts with a copy of the saved menu, ready for
guests. Scanning the menu again updates the copied dishes in place and notes
what changed (see `saved_items` and `describe_change`, used by
menu.apply_extraction); dishes the new scan didn't find are listed for the
organiser to remove. The restaurant's own page rescans the same way.

Each dinner keeps its own copy, so updating a restaurant's menu never changes
an old bill.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from app.models import Dinner, MenuCategory, MenuItem, MenuPage, Restaurant, utcnow
from app.services.images import dinner_dir, image_path
from app.services.money import fmt

if TYPE_CHECKING:
    from app.services.menu import Owner

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


def folder(restaurant: Restaurant) -> str:
    """Where the restaurant's menu photos live under the upload directory."""
    return f"r{restaurant.id}"


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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


def _about(s: Session, r: Restaurant) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "menu_updated_at": r.menu_updated_at.isoformat() if r.menu_updated_at else None,
        "dishes": len(_published(s, r.id, restaurant=True)[2]),
    }


def summary(s: Session, dinner: Dinner) -> dict | None:
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    return _about(s, r) if r else None


def listing(s: Session) -> list[dict]:
    """Places you've eaten, most recently visited first, for one-tap starts."""
    visits: dict[str, list] = {}
    for rid, at in s.exec(
        select(Dinner.restaurant_id, Dinner.created_at).where(Dinner.restaurant_id != None)  # noqa: E711
    ).all():
        visits.setdefault(rid, []).append(at)
    out = [
        {
            **_about(s, r),
            "visits": len(visits.get(r.id, [])),
            "last_visit": max(visits[r.id]).isoformat() if visits.get(r.id) else None,
        }
        for r in s.exec(select(Restaurant)).all()
    ]
    out.sort(key=lambda x: x["name"].lower())
    out.sort(key=lambda x: x["last_visit"] or "", reverse=True)
    return out


# ------------------------------------------------------------------ copying
def _published(s: Session, owner_id: str, *, restaurant: bool) -> tuple[list, list, list]:
    """The menu as guests would get it: published pages (no specials), their
    dishes and hand-typed ones (not sold out, not specials), and the sections used."""

    def mine(model):
        return model.restaurant_id == owner_id if restaurant else model.dinner_id == owner_id

    pages = s.exec(
        select(MenuPage)
        .where(mine(MenuPage), MenuPage.status == "published", MenuPage.kind != "specials")
        .order_by(col(MenuPage.sort))
    ).all()
    page_ids = {p.id for p in pages}
    items = [
        i
        for i in s.exec(select(MenuItem).where(mine(MenuItem)).order_by(col(MenuItem.sort))).all()
        if not i.is_special and (i.page_id in page_ids or (i.page_id is None and not i.unavailable))
    ]
    cat_ids = {i.category_id for i in items if i.category_id}
    cats = [
        c
        for c in s.exec(select(MenuCategory).where(mine(MenuCategory)).order_by(col(MenuCategory.sort))).all()
        if c.id in cat_ids
    ]
    return list(pages), cats, items


def _copy(s: Session, src: tuple, src_folder: str, owner: dict, dst_folder: str, *, from_saved: bool) -> int:
    """Copy pages, sections and dishes (with their photos) to a new owner."""
    pages, cats, items = src
    target = dinner_dir(dst_folder)
    page_ids: dict[str, str] = {}
    for n, p in enumerate(pages):
        image = p.image_file
        source = image_path(src_folder, image) if image else None
        if source is None:
            image = None
        elif not (target / image).exists():
            shutil.copyfile(source, target / image)
        page = MenuPage(
            **owner,
            kind="menu",
            image_file=image,
            status="published",
            legend=p.legend or [],
            flags=p.flags or [],
            from_saved=from_saved,
            sort=n,
        )
        s.add(page)
        page_ids[p.id] = page.id
    cat_ids: dict[str, str] = {}
    for n, c in enumerate(cats):
        cat = MenuCategory(**owner, name=c.name, note=c.note, extras=c.extras or [], sort=n)
        s.add(cat)
        cat_ids[c.id] = cat.id
    for i in items:
        s.add(
            MenuItem(
                **owner,
                page_id=page_ids.get(i.page_id),
                category_id=cat_ids.get(i.category_id),
                from_saved=from_saved,
                **{f: getattr(i, f) for f in ITEM_COPY},
            )
        )
    return len(items)


def _clear(s: Session, r: Restaurant) -> None:
    for model in (MenuItem, MenuCategory, MenuPage):
        for row in s.exec(select(model).where(model.restaurant_id == r.id)).all():
            s.delete(row)
    s.flush()


# ------------------------------------------------------------------- saving
def save_menu(s: Session, dinner: Dinner) -> None:
    """Save the dinner's published menu as its restaurant's saved menu.

    Only the restaurant's newest dinner writes, so tidying an old dinner later
    can't roll the saved menu back. Nor does a dinner overwrite edits made on
    the restaurant's own page after it started, or one being made there now.
    A dinner with nothing published leaves the saved menu alone.
    """
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    if r is None or dinner.is_demo:
        return
    newest = s.exec(
        select(Dinner.id).where(Dinner.restaurant_id == r.id).order_by(col(Dinner.created_at).desc())
    ).first()
    if newest != dinner.id:
        return
    edited_since = r.menu_updated_at and _utc(r.menu_updated_at) > _utc(dinner.created_at)
    if r.menu_dinner_id != dinner.id and edited_since:
        return
    if s.exec(
        select(MenuPage.id).where(MenuPage.restaurant_id == r.id, col(MenuPage.status).in_(["processing", "review"]))
    ).first():
        return
    src = _published(s, dinner.id, restaurant=False)
    if not src[2]:
        return
    _clear(s, r)
    _copy(s, src, dinner.id, {"restaurant_id": r.id}, folder(r), from_saved=False)
    r.menu_dinner_id = dinner.id
    r.menu_updated_at = utcnow()
    s.add(r)


# ------------------------------------------------------------------ loading
def load_menu(s: Session, dinner: Dinner) -> int:
    """Give a new dinner a copy of its restaurant's saved menu. Returns the dish count."""
    r = s.get(Restaurant, dinner.restaurant_id) if dinner.restaurant_id else None
    if r is None:
        return 0
    src = _published(s, r.id, restaurant=True)
    if not src[2]:
        return 0
    return _copy(s, src, folder(r), {"dinner_id": dinner.id}, dinner.id, from_saved=True)


# ----------------------------------------------------------------- rescans
def start_rescan(s: Session, owner: Owner) -> None:
    """New menu photos for a restaurant's own menu: until the new scan finds
    them, its current pages and dishes count as "saved" — matched dishes are
    updated in place, and the rest are listed for removal, as at a dinner."""
    unfinished = s.exec(
        select(MenuPage.id).where(owner.of(MenuPage), col(MenuPage.status).in_(["processing", "review"]))
    ).first()
    if unfinished:
        return  # more pages of the scan already under way
    for model in (MenuPage, MenuItem):
        for row in s.exec(select(model).where(owner.of(model))).all():
            row.from_saved = True
            if model is MenuItem:
                row.change_note = ""
            s.add(row)


def saved_items(s: Session, owner: Owner) -> dict[str, MenuItem]:
    """Saved dishes a fresh scan hasn't found yet, by loose name."""
    rows = s.exec(select(MenuItem).where(owner.of(MenuItem), MenuItem.from_saved == True)).all()  # noqa: E712
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


# ---------------------------------------------------------------- start-up
def _adopt_json(s: Session, r: Restaurant) -> None:
    """A menu saved before restaurants had their own rows: turn it into rows."""
    m = r.menu
    pages: dict[str, str] = {}
    for n, p in enumerate(m.get("pages", [])):
        page = MenuPage(
            restaurant_id=r.id,
            kind="menu",
            image_file=p.get("image"),
            status="published",
            legend=p.get("legend") or [],
            flags=p.get("flags") or [],
            sort=n,
        )
        s.add(page)
        pages[p["key"]] = page.id
    cats: dict[str, str] = {}
    for n, c in enumerate(m.get("categories", [])):
        cat = MenuCategory(
            restaurant_id=r.id, name=c["name"], note=c.get("note", ""), extras=c.get("extras") or [], sort=n
        )
        s.add(cat)
        cats[c["key"]] = cat.id
    for x in m.get("items", []):
        s.add(
            MenuItem(
                restaurant_id=r.id,
                page_id=pages.get(x.get("page")),
                category_id=cats.get(x.get("category")),
                **{f: x[f] for f in ITEM_COPY if f in x},
            )
        )
    r.menu = {}
    s.add(r)


def link_existing() -> None:
    """One-off on start-up: dinners from before restaurants existed get linked,
    each restaurant's newest dinner provides its saved menu, and menus saved
    the old way (as JSON on the restaurant) become rows."""
    from app.db import locked_write

    with locked_write() as s:
        for r in s.exec(select(Restaurant)).all():
            if r.menu:
                _adopt_json(s, r)
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
