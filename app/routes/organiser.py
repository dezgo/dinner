"""Organiser pages and API: /o/… and /api/o/… (all require sign-in)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.db import get_session, locked_write, session_scope
from app.models import Dinner, MenuCategory, MenuItem, MenuPage, Participant, Receipt, utcnow
from app.security import is_organiser, password_ok, rate_limit, require_organiser, sign_in, sign_out
from app.services import dinners, finalise, menu, payments, reconcile, restaurants, up
from app.services.billing import active_receipt, bill_for
from app.services.events import ORGANISER_CHANNEL, audit, broker, touch
from app.services.images import ImageRejected, split_uploads, store_page
from app.services.orders import Actor
from app.services.qr import qr_svg
from app.services.state import organiser_state, payment_profile
from app.templating import render

router = APIRouter()
api = APIRouter(prefix="/api/o", dependencies=[Depends(require_organiser)])

PAYID_TYPES = {"phone", "email", "abn", "org_id"}


def base_url(request: Request) -> str:
    return get_settings().public_base_url.rstrip("/") or str(request.base_url).rstrip("/")


def _dinner(s: Session, dinner_id: str) -> Dinner:
    d = s.get(Dinner, dinner_id)
    if d is None:
        raise HTTPException(404, "Dinner not found.")
    return d


def _menu_changed(s: Session, d: Dinner) -> None:
    """Tell screens the menu moved, and keep the restaurant's saved copy current."""
    restaurants.save_menu(s, d)
    touch(s, d, "menu")


def _organiser_actor(s: Session, dinner: Dinner) -> Actor:
    me = s.get(Participant, dinner.organiser_participant_id) if dinner.organiser_participant_id else None
    return Actor(me, True)


# -------------------------------------------------------------------- pages
@router.get("/o/login")
def login_page(request: Request):
    if is_organiser(request):
        return RedirectResponse("/o", 303)
    return render(request, "login.html", {"configured": bool(get_settings().admin_password)})


@router.get("/o")
def dashboard(request: Request):
    if not is_organiser(request):
        return RedirectResponse("/o/login", 303)
    return render(request, "organiser.html", {"view": "home"})


@router.get("/o/settings")
def settings_page(request: Request):
    if not is_organiser(request):
        return RedirectResponse("/o/login", 303)
    return render(request, "organiser.html", {"view": "settings"})


@router.get("/o/d/{dinner_id}")
def dinner_page(dinner_id: str, request: Request):
    if not is_organiser(request):
        return RedirectResponse("/o/login", 303)
    return render(request, "organiser.html", {"view": "dinner", "dinner_id": dinner_id})


@router.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    rate_limit(request, "login", 5, 300)
    if not password_ok(str(body.get("password", ""))):
        raise HTTPException(401, "That password isn't right.")
    sign_in(response)
    return {"ok": True}


@router.post("/api/logout")
def logout(response: Response):
    sign_out(response)
    return {"ok": True}


# ------------------------------------------------------------------ dinners
@api.get("/dinners")
def list_dinners(archived: bool = False, s: Session = Depends(get_session)):
    rows = s.exec(select(Dinner).where(Dinner.archived == archived).order_by(col(Dinner.created_at).desc())).all()
    out = []
    for d in rows:
        st = payments.statuses(s, d)
        waiting = sum(1 for v in st.values() if v["state"] in ("awaiting", "marked_sent", "part_paid"))
        out.append(
            {
                "id": d.id,
                "code": d.code,
                "restaurant_name": d.restaurant_name,
                "status": d.status,
                "is_demo": d.is_demo,
                "created_at": d.created_at.isoformat(),
                "people": len(st),
                "waiting_on": waiting if d.instructions_issued_at else None,
            }
        )
    review = len([t for t in payments.review_queue(s, include_unmatched=False) if not t.is_simulated])
    archived_count = s.exec(select(func.count()).select_from(Dinner).where(Dinner.archived == True)).one()  # noqa: E712
    return {
        "dinners": out,
        "archived_count": archived_count,
        "review_count": review,
        "profile_ready": bool(payment_profile(s).get("payid")),
    }


@api.post("/dinners")
def create_dinner(body: dict = Body(...)):
    with locked_write() as s:
        profile = payment_profile(s)
        d = dinners.create_dinner(
            s, body.get("restaurant_name", ""), body.get("my_name") or profile.get("my_name") or "Organiser"
        )
        restaurants.link(s, d)
        dishes = restaurants.load_menu(s, d) if body.get("use_saved_menu", True) else 0
        if dishes:
            audit(s, d.id, "organiser", f"Started with the saved menu ({dishes} dishes)")
    return {"id": d.id}


@api.get("/restaurants")
def list_restaurants(s: Session = Depends(get_session)):
    return {"restaurants": restaurants.listing(s)}


@api.post("/demo")
def create_demo():
    from app.services.demo import create_demo_dinner

    return {"id": create_demo_dinner()}


@api.post("/d/{dinner_id}/demo-receipt")
def demo_receipt(dinner_id: str):
    from app.services.demo import scan_sample_receipt

    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if d.status == "finalised":
            raise HTTPException(423, "Reopen the bill to scan a receipt.")
        old = active_receipt(s, d.id)
        if old is not None and old.status == "processing":
            raise HTTPException(409, "The previous receipt is still being read.")
        if old is not None:
            old.status = "discarded"
            s.add(old)
    return {"id": scan_sample_receipt(dinner_id)}


@api.get("/demo/{which}.png")
def demo_image(which: str):
    """The sample photos, to try the real upload path by hand."""
    from app.services import demo

    makers = {"menu-1": demo.menu_page_1, "menu-2": demo.menu_page_2, "receipt": demo.receipt_image}
    if which not in makers:
        raise HTTPException(404, "Not found.")
    return Response(makers[which](), media_type="image/png")


@api.get("/d/{dinner_id}/state")
def dinner_state(dinner_id: str, request: Request, s: Session = Depends(get_session)):
    d = _dinner(s, dinner_id)
    state = organiser_state(s, d)
    state["share_url"] = f"{base_url(request)}/d/{d.public_token}"
    state["public_token"] = d.public_token
    state["demo_tools"] = get_settings().demo_tools and d.is_demo
    state["extraction_enabled"] = get_settings().extraction_enabled
    return state


@api.patch("/d/{dinner_id}")
def edit_dinner(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if "restaurant_name" in body:
            d.restaurant_name = " ".join(str(body["restaurant_name"]).split())[:80]
            restaurants.link(s, d)
        if "archived" in body and bool(body["archived"]) != d.archived:
            d.archived = bool(body["archived"])
            audit(
                s,
                d.id,
                "organiser",
                "Cleared from the dinner list" if d.archived else "Brought back to the dinner list",
            )
        touch(s, d)
    return {"ok": True}


@api.get("/d/{dinner_id}/qr.svg")
def qr(dinner_id: str, request: Request, s: Session = Depends(get_session)):
    d = _dinner(s, dinner_id)
    return Response(
        qr_svg(f"{base_url(request)}/d/{d.public_token}"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------- menu
@api.post("/d/{dinner_id}/pages")
async def upload_pages(dinner_id: str, files: list[UploadFile] = File(...), kind: str = Form("menu")):
    if kind not in ("menu", "specials"):
        raise HTTPException(422, "Unknown page type.")
    blobs = [await f.read() for f in files[:12]]
    with session_scope() as s:
        _dinner(s, dinner_id)
    try:
        names = [store_page(dinner_id, p) for p in split_uploads(blobs, 12)]
    except ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    ids = []
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        start = len(s.exec(select(MenuPage.id).where(MenuPage.dinner_id == d.id)).all())
        for i, name in enumerate(names):
            page = MenuPage(dinner_id=d.id, kind=kind, image_file=name, sort=start + i)
            s.add(page)
            ids.append(page.id)
        _menu_changed(s, d)
    for pid in ids:
        menu.queue_page(pid)
    return {"pages": ids}


@api.post("/d/{dinner_id}/pages/{page_id}/{action}")
def page_action(dinner_id: str, page_id: str, action: str):
    requeue = False
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        page = s.get(MenuPage, page_id)
        if page is None or page.dinner_id != d.id:
            raise HTTPException(404, "Page not found.")
        if action == "publish":
            if page.status not in ("review", "published"):
                raise HTTPException(409, "This page isn't ready to publish.")
            # Remaining flags stay visible to guests ("price unclear — check
            # the photo"); publishing means "good enough to browse".
            page.status = "published"
        elif action == "unpublish":
            page.status = "review"
        elif action == "retry":
            if page.status not in ("failed",):
                raise HTTPException(409, "Only failed pages can be retried.")
            page.status, page.error = "processing", None
            requeue = True
        elif action == "manual":
            page.status = "published"
            page.error = None
        else:
            raise HTTPException(404, "Unknown action.")
        s.add(page)
        _menu_changed(s, d)
    if requeue:
        menu.queue_page(page_id)
    return {"ok": True}


@api.delete("/d/{dinner_id}/pages/{page_id}")
def delete_page(dinner_id: str, page_id: str):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        page = s.get(MenuPage, page_id)
        if page is None or page.dinner_id != d.id:
            raise HTTPException(404, "Page not found.")
        for item in s.exec(select(MenuItem).where(MenuItem.page_id == page.id)).all():
            _delete_or_hide(s, item)
        s.delete(page)
        _menu_changed(s, d)
    return {"ok": True}


@api.post("/d/{dinner_id}/menu/drop-unseen")
def drop_unseen(dinner_id: str):
    """After a rescan: remove saved dishes the new scan didn't find, and saved pages left empty."""
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        gone = s.exec(select(MenuItem).where(MenuItem.dinner_id == d.id, MenuItem.from_saved == True)).all()  # noqa: E712
        for item in gone:
            _delete_or_hide(s, item)
        s.flush()
        for page in s.exec(select(MenuPage).where(MenuPage.dinner_id == d.id, MenuPage.from_saved == True)).all():  # noqa: E712
            if not s.exec(select(MenuItem.id).where(MenuItem.page_id == page.id)).first():
                s.delete(page)
        audit(s, d.id, "organiser", f"Removed {len(gone)} saved dishes not on the new scan")
        _menu_changed(s, d)
    return {"removed": len(gone)}


def _delete_or_hide(s: Session, item: MenuItem) -> None:
    from app.models import OrderLine

    used = s.exec(select(OrderLine).where(OrderLine.menu_item_id == item.id)).first()
    if used:
        # Keep it so recorded orders still point at something; hide it.
        item.unavailable = True
        item.page_id = None
        item.version += 1
        s.add(item)
    else:
        s.delete(item)


@api.post("/d/{dinner_id}/categories")
def add_category(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        name = str(body.get("name", "")).strip()[:80]
        if not name:
            raise HTTPException(422, "Name the section.")
        count = len(s.exec(select(MenuCategory.id).where(MenuCategory.dinner_id == d.id)).all())
        cat = MenuCategory(dinner_id=d.id, name=name, sort=count, extras=menu.clean_options(body.get("extras")))
        s.add(cat)
        _menu_changed(s, d)
    return {"id": cat.id}


@api.patch("/d/{dinner_id}/categories/{cat_id}")
def edit_category(dinner_id: str, cat_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        cat = s.get(MenuCategory, cat_id)
        if cat is None or cat.dinner_id != d.id:
            raise HTTPException(404, "Section not found.")
        if "name" in body:
            cat.name = str(body["name"]).strip()[:80] or cat.name
        if "note" in body:
            cat.note = str(body["note"]).strip()[:300]
        if "extras" in body:
            cat.extras = menu.clean_options(body["extras"])
        s.add(cat)
        _menu_changed(s, d)
    return {"ok": True}


@api.post("/d/{dinner_id}/items")
def add_item(dinner_id: str, body: dict = Body(...)):
    """Manual entry: specials, corrections, or a menu that couldn't be read."""
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if not str(body.get("name", "")).strip():
            raise HTTPException(422, "A dish needs a name.")
        item = MenuItem(dinner_id=d.id, name="(new)", source="manual", sort=menu._next_item_sort(s, d.id) + 1)
        if body.get("category_id"):
            cat = s.get(MenuCategory, body["category_id"])
            if cat is None or cat.dinner_id != d.id:
                raise HTTPException(404, "Section not found.")
        menu.update_item(s, item, body)
        item.version = 1
        s.add(item)
        _menu_changed(s, d)
    return {"id": item.id}


@api.patch("/d/{dinner_id}/items/{item_id}")
def edit_item(dinner_id: str, item_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        item = s.get(MenuItem, item_id)
        if item is None or item.dinner_id != d.id:
            raise HTTPException(404, "Dish not found.")
        menu.check_version(item, body.pop("version", None))
        menu.update_item(s, item, body)
        audit(s, d.id, "organiser", f"Edited menu item {item.name}")
        _menu_changed(s, d)
    return {"ok": True, "version": item.version}


@api.delete("/d/{dinner_id}/items/{item_id}")
def delete_item(dinner_id: str, item_id: str):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        item = s.get(MenuItem, item_id)
        if item is None or item.dinner_id != d.id:
            raise HTTPException(404, "Dish not found.")
        _delete_or_hide(s, item)
        _menu_changed(s, d)
    return {"ok": True}


# ------------------------------------------------------------------- people
@api.post("/d/{dinner_id}/people")
def add_person(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        p = dinners.add_participant(s, d, body.get("name", ""))
    return {"id": p.id, "reference": p.reference}


@api.patch("/d/{dinner_id}/people/{pid}")
def rename_person(dinner_id: str, pid: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        p = s.get(Participant, pid)
        if p is None or p.dinner_id != d.id:
            raise HTTPException(404, "Person not found.")
        dinners.rename(s, d, p, body.get("name", ""))
    return {"ok": True}


@api.delete("/d/{dinner_id}/people/{pid}")
def remove_person(dinner_id: str, pid: str):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        p = s.get(Participant, pid)
        if p is None or p.dinner_id != d.id:
            raise HTTPException(404, "Person not found.")
        dinners.remove(s, d, p)
    return {"ok": True}


@api.post("/d/{dinner_id}/people/{pid}/link")
def personal_link(dinner_id: str, pid: str, request: Request):
    """A one-time link that signs this person in on a new phone."""
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        p = s.get(Participant, pid)
        if p is None or p.dinner_id != d.id or p.removed:
            raise HTTPException(404, "Person not found.")
        token = dinners.issue_recovery(s, p)
        audit(s, d.id, "organiser", f"Issued a personal link for {p.display_name}")
    return {"url": f"{base_url(request)}/d/{d.public_token}/r/{token}"}


# --------------------------------------------------------------------- bill
@api.post("/d/{dinner_id}/total")
def set_total(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        cents = body.get("cents")
        finalise.set_total(
            s, d, body.get("version"), None if cents is None else int(cents), body.get("source", "quoted")
        )
    return {"ok": True}


@api.post("/d/{dinner_id}/finalise")
def do_finalise(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if not payment_profile(s).get("payid") and not d.is_demo:
            raise HTTPException(409, "Add your PayID in Settings first, so guests know where to pay.")
        return finalise.finalise(s, d, int(body.get("revision", -1)))


@api.post("/d/{dinner_id}/reopen")
def do_reopen(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        return finalise.reopen(s, d, body.get("version"), bool(body.get("acknowledged")))


# ------------------------------------------------------------------ receipt
@api.post("/d/{dinner_id}/receipt")
async def upload_receipt(dinner_id: str, files: list[UploadFile] = File(...)):
    blobs = [await f.read() for f in files[:4]]
    try:
        names = [store_page(dinner_id, (image, None)) for image, _ in split_uploads(blobs, 4)]
    except ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if d.status == "finalised":
            raise HTTPException(423, "Reopen the bill to scan a receipt.")
        old = active_receipt(s, d.id)
        if old is not None:
            if old.status == "processing":
                raise HTTPException(409, "The previous receipt is still being read.")
            old.status = "discarded"
            s.add(old)
        r = Receipt(dinner_id=d.id, image_files=names)
        s.add(r)
        touch(s, d, "receipt")
    reconcile.queue_receipt(r.id)
    return {"id": r.id}


@api.post("/d/{dinner_id}/receipt/discard")
def discard_receipt(dinner_id: str):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        r = active_receipt(s, d.id)
        if r is not None:
            r.status = "discarded"
            s.add(r)
            if d.bill_total_source == "receipt" and d.status == "open":
                d.bill_total_cents, d.bill_total_source = None, None
                d.version += 1
        touch(s, d, "receipt")
    return {"ok": True}


@api.post("/d/{dinner_id}/receipt/resuggest")
def resuggest(dinner_id: str):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        r = active_receipt(s, d.id)
        if r is None or r.status != "review":
            raise HTTPException(409, "No receipt to reconcile.")
        reconcile.resuggest(s, d, r)
    return {"ok": True}


@api.post("/d/{dinner_id}/receipt/add-all")
def add_all(dinner_id: str):
    """Receipt-first: put every unresolved receipt line on the bill to be claimed."""
    from app.models import ReceiptLine

    with locked_write() as s:
        d = _dinner(s, dinner_id)
        r = active_receipt(s, d.id)
        if r is None or r.status != "review":
            raise HTTPException(409, "No receipt to add from.")
        actor = _organiser_actor(s, d)
        pending = s.exec(
            select(ReceiptLine).where(ReceiptLine.receipt_id == r.id, ReceiptLine.state == "pending")
        ).all()
        blocked = [x.description for x in pending if x.line_total_cents is None]
        if blocked:
            raise HTTPException(422, f"Enter amounts first for: {', '.join(blocked)}.")
        for x in pending:
            reconcile.add_as_new(s, d, x.id, actor)
    return {"ok": True}


@api.post("/d/{dinner_id}/receipt/lines/{rid}/{action}")
def receipt_line_action(dinner_id: str, rid: str, action: str, body: dict = Body(default={})):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if action == "match":
            reconcile.confirm_match(s, d, rid, list(body.get("line_ids", [])), bool(body.get("use_receipt")))
        elif action == "add":
            reconcile.add_as_new(s, d, rid, _organiser_actor(s, d))
        elif action == "ignore":
            reconcile.ignore(s, d, rid)
        elif action == "reset":
            reconcile.reset(s, d, rid)
        elif action == "edit":
            reconcile.edit_receipt_line(s, d, rid, body)
        else:
            raise HTTPException(404, "Unknown action.")
    return {"ok": True}


# ----------------------------------------------------------------- payments
@api.post("/d/{dinner_id}/payments")
def manual_payment(dinner_id: str, body: dict = Body(...)):
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        pay = payments.record_manual(
            s, d, body.get("participant_id", ""), int(body.get("amount_cents", 0)), str(body.get("note", ""))
        )
    return {"id": pay.id}


@api.post("/payments/{payment_id}/undo")
def undo_payment(payment_id: str, body: dict = Body(default={})):
    with locked_write() as s:
        payments.undo_payment(s, payment_id, str(body.get("reason", "")))
    return {"ok": True}


@api.post("/tx/{tx_id}/confirm")
def confirm_tx(tx_id: str, body: dict = Body(...)):
    with locked_write() as s:
        pay = payments.confirm_transaction(s, tx_id, body.get("participant_id", ""))
    return {"id": pay.id}


@api.post("/tx/{tx_id}/ignore")
def ignore_tx(tx_id: str):
    with locked_write() as s:
        payments.ignore_transaction(s, tx_id)
    return {"ok": True}


@api.get("/review")
def review(s: Session = Depends(get_session)):
    from app.services.state import _tx

    return {"transactions": [_tx(t) for t in payments.review_queue(s) if not t.is_simulated]}


@api.post("/d/{dinner_id}/simulate-payment")
def simulate_payment(dinner_id: str, body: dict = Body(...)):
    """Demo dinners only: pretend a transfer arrived, through the real matcher."""
    from app.models import new_id

    if not get_settings().demo_tools:
        raise HTTPException(404, "Demo tools are off.")
    with locked_write() as s:
        d = _dinner(s, dinner_id)
        if not d.is_demo:
            raise HTTPException(403, "Simulated payments only work on demo dinners.")
        tx = payments.ingest(
            s,
            {
                "id": f"sim-{new_id()}",
                "status": "SETTLED",
                "amount_cents": int(body.get("amount_cents", 0)),
                "description": str(body.get("description", ""))[:200],
                "message": str(body.get("message", ""))[:280],
                "raw_text": "",
                "created_at": utcnow(),
            },
            simulated=True,
        )
        if tx is None:
            raise HTTPException(409, "No payment window is open (finalise the demo bill first).")
        return {"match_state": tx.match_state, "note": tx.match_note}


# ----------------------------------------------------------------- settings
@api.get("/settings")
def get_org_settings(request: Request, s: Session = Depends(get_session)):
    cfg = get_settings()
    return {
        "profile": payment_profile(s),
        "up": {
            "token_configured": cfg.up_enabled,
            "account_id": up.get_setting(s, "up_account_id"),
            "account_name": up.get_setting(s, "up_account_name"),
            "webhook_id": up.get_setting(s, "up_webhook_id"),
            "webhook_url": up.get_setting(s, "up_webhook_url"),
            "last_sync": up.get_setting(s, "up_last_sync"),
            "last_error": up.get_setting(s, "up_last_error"),
            "suggested_webhook_url": base_url(request) + "/webhooks/up",
        },
        "extraction_enabled": cfg.extraction_enabled,
        "extraction_model": cfg.extraction_model,
    }


@api.put("/settings/profile")
def save_profile(body: dict = Body(...)):
    payid = str(body.get("payid", "")).strip()[:120]
    payid_type = body.get("payid_type", "phone")
    if payid_type not in PAYID_TYPES:
        raise HTTPException(422, "Choose a PayID type.")
    bsb = "".join(ch for ch in str(body.get("bsb", "")) if ch.isdigit())
    acct = "".join(ch for ch in str(body.get("account_number", "")) if ch.isdigit())
    if bsb and len(bsb) != 6:
        raise HTTPException(422, "A BSB is 6 digits.")
    if acct and not 5 <= len(acct) <= 10:
        raise HTTPException(422, "That account number doesn't look right.")
    profile = {
        "payid": payid,
        "payid_type": payid_type,
        "recipient_name": str(body.get("recipient_name", "")).strip()[:80],
        "bsb": f"{bsb[:3]}-{bsb[3:]}" if bsb else "",
        "account_number": acct,
        "my_name": str(body.get("my_name", "")).strip()[:40],
    }
    with locked_write() as s:
        up.put_setting(s, "payment_profile", profile)
    return {"ok": True}


@api.get("/up/accounts")
def up_accounts():
    try:
        return {"accounts": up.UpClient().accounts()}
    except up.UpError as e:
        raise HTTPException(502, str(e)) from e


@api.put("/up/account")
def up_choose_account(body: dict = Body(...)):
    try:
        accounts = {a["id"]: a for a in up.UpClient().accounts()}
    except up.UpError as e:
        raise HTTPException(502, str(e)) from e
    chosen = accounts.get(body.get("id"))
    if chosen is None:
        raise HTTPException(404, "Account not found.")
    with locked_write() as s:
        up.put_setting(s, "up_account_id", chosen["id"])
        up.put_setting(s, "up_account_name", chosen["name"])
    return {"ok": True}


@api.post("/up/webhook")
def up_webhook(request: Request):
    try:
        return up.register_webhook(base_url(request))
    except up.UpError as e:
        raise HTTPException(502, str(e)) from e


@api.post("/up/sync")
def up_sync():
    try:
        return up.sync()
    except up.UpError as e:
        raise HTTPException(502, str(e)) from e


@api.get("/events")
async def organiser_events():
    return StreamingResponse(
        broker.stream(ORGANISER_CHANNEL),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@api.get("/d/{dinner_id}/bill")
def bill(dinner_id: str, s: Session = Depends(get_session)):
    return bill_for(s, _dinner(s, dinner_id)).as_dict()
