"""A read-only copy of a dinner's menu, to show (or hand) to restaurant staff.

The link carries a signed dinner id, so it can't be guessed or altered, and
it opens nothing but the published menu: no names, orders, bill or way into
the dinner itself.
"""

from __future__ import annotations

from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeSerializer
from sqlmodel import Session

from app.config import get_settings
from app.models import Dinner


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="dinnertab-menu-share")


def path_for(dinner_id: str) -> str:
    return f"/m/{_signer().dumps(dinner_id)}"


def dinner_for(s: Session, token: str) -> Dinner:
    try:
        dinner_id = _signer().loads(token)
    except BadSignature as e:
        raise HTTPException(404, "This menu link isn't valid.") from e
    dinner = s.get(Dinner, dinner_id)
    if dinner is None:
        raise HTTPException(404, "This menu is no longer available.")
    return dinner


def sections(menu: dict) -> list[dict]:
    """The published menu grouped the way it's printed: sections in order, dishes in order."""
    cats = {c["id"]: c for c in menu["categories"]}
    out = [{**c, "items": [i for i in menu["items"] if i["category_id"] == c["id"]]} for c in menu["categories"]]
    loose = [i for i in menu["items"] if i["category_id"] not in cats]
    if loose:
        out.append({"id": "more", "name": "More", "note": "", "extras": [], "items": loose})
    return [c for c in out if c["items"]]


def legend(menu: dict) -> list[dict]:
    seen: set[str] = set()
    return [e for e in menu["legend"] if not (e["symbol"] in seen or seen.add(e["symbol"]))]
