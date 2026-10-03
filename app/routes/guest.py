"""Guest pages and API: /d/{token} and /api/d/{token}/…

The organiser uses these same endpoints (with their organiser cookie) when
recording or correcting orders, so there is one code path for every change.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session, locked_write, session_scope
from app.models import MenuItem, MenuPage, Participant, Receipt, Shortlist
from app.security import (
    GuestContext,
    dinner_by_token,
    guest_context,
    is_organiser,
    issue_guest_session,
    participant_from_request,
    rate_limit,
)
from app.services import dinners, menu_share, orders, payments
from app.services.events import broker, dinner_channel
from app.services.images import image_path
from app.services.qr import qr_svg
from app.services.state import guest_menu, guest_state
from app.templating import render

router = APIRouter()


def _actor(s: Session, ctx: GuestContext) -> orders.Actor:
    participant = ctx.participant
    if participant is not None:
        participant = s.get(Participant, participant.id)
    return orders.Actor(participant, ctx.organiser)


def _fresh(s: Session, request: Request, public_token: str) -> tuple:
    """Re-read the dinner and caller inside a locked write."""
    dinner = dinner_by_token(s, public_token)
    me = participant_from_request(s, request, dinner)
    org = is_organiser(request)
    if me is None and org and dinner.organiser_participant_id:
        me = s.get(Participant, dinner.organiser_participant_id)
    return dinner, orders.Actor(me, org)


def _once(s: Session, dinner, op_id: str | None, fn):
    """Run `fn` once per client op id; a retry gets the first result back."""
    if (prior := orders.replay(s, op_id, dinner)) is not None:
        return prior
    return orders.remember(s, op_id, dinner, fn())


# -------------------------------------------------------------------- pages
@router.get("/d/{public_token}")
def guest_page(request: Request, ctx: GuestContext = Depends(guest_context)):
    return render(
        request, "guest.html", {"token": ctx.dinner.public_token, "title": ctx.dinner.restaurant_name or "Dinner"}
    )


@router.get("/d/{public_token}/r/{recovery}")
def recover(public_token: str, recovery: str, request: Request):
    rate_limit(request, "recover", 10, 300)
    with locked_write() as s:
        dinner = dinner_by_token(s, public_token)
        p = dinners.redeem_recovery(s, dinner, recovery)
        response = RedirectResponse(f"/d/{public_token}", status_code=303)
        issue_guest_session(response, dinner, p)
        s.add(p)
    return response


@router.get("/m/{token}")
def shared_menu(token: str, request: Request, show: bool = False):
    """The read-only menu for restaurant staff. `show=1` is the in-person version:
    a QR code for their own phone, and (for people at the dinner) a way back."""
    with session_scope() as s:
        dinner = menu_share.dinner_for(s, token)
        menu = guest_menu(s, dinner)
        back = None
        if show and (is_organiser(request) or participant_from_request(s, request, dinner) is not None):
            back = f"/d/{dinner.public_token}"
    base = get_settings().public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return render(
        request,
        "menu.html",
        {
            "name": dinner.restaurant_name or "Menu",
            "sections": menu_share.sections(menu),
            "legend": menu_share.legend(menu),
            "back": back,
            "qr": qr_svg(f"{base}/m/{token}").decode() if show else None,
        },
    )


# ---------------------------------------------------------------------- api
@router.get("/api/d/{public_token}/state")
def state(ctx: GuestContext = Depends(guest_context), s: Session = Depends(get_session)):
    me = ctx.participant
    if me is None and ctx.organiser and ctx.dinner.organiser_participant_id:
        me = s.get(Participant, ctx.dinner.organiser_participant_id)
    return guest_state(s, ctx.dinner, me, organiser=ctx.organiser)


@router.post("/api/d/{public_token}/join")
def join(public_token: str, request: Request, response: Response, body: dict = Body(...)):
    rate_limit(request, "join", 20, 600)
    with locked_write() as s:
        dinner = dinner_by_token(s, public_token)
        if participant_from_request(s, request, dinner) is not None:
            raise HTTPException(409, "You've already joined on this phone.")
        p = dinners.add_participant(s, dinner, body.get("name", ""))
        issue_guest_session(response, dinner, p)
    return {"participant_id": p.id, "reference": p.reference}


@router.post("/api/d/{public_token}/me")
def rename_me(public_token: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        if actor.participant is None:
            raise HTTPException(401, "Join the dinner first.")
        dinners.rename(s, dinner, actor.participant, body.get("name", ""))
    return {"ok": True}


@router.post("/api/d/{public_token}/lines")
def record(public_token: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        if actor.participant is None and not actor.organiser:
            raise HTTPException(401, "Join the dinner first.")
        return orders.record_line(s, dinner, actor, body)


@router.post("/api/d/{public_token}/lines/{line_id}/join")
def join_line(public_token: str, line_id: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        return _once(
            s,
            dinner,
            body.get("op_id"),
            lambda: orders.join_line(s, dinner, actor, line_id, body.get("version"), body.get("units", 1)),
        )


@router.post("/api/d/{public_token}/lines/{line_id}/leave")
def leave_line(public_token: str, line_id: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        return _once(
            s,
            dinner,
            body.get("op_id"),
            lambda: orders.leave_line(s, dinner, actor, line_id, body.get("version"), body.get("participant_id")),
        )


@router.patch("/api/d/{public_token}/lines/{line_id}")
def edit_line(public_token: str, line_id: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        version = body.pop("version", None)
        op_id = body.pop("op_id", None)
        return _once(s, dinner, op_id, lambda: orders.update_line(s, dinner, actor, line_id, version, body))


@router.put("/api/d/{public_token}/lines/{line_id}/allocations")
def allocate(public_token: str, line_id: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        return _once(
            s,
            dinner,
            body.get("op_id"),
            lambda: orders.set_allocations(
                s, dinner, actor, line_id, body.get("version"), body.get("allocations", []), body.get("split_mode")
            ),
        )


@router.delete("/api/d/{public_token}/lines/{line_id}")
def remove_line(public_token: str, line_id: str, version: int, request: Request, op_id: str | None = None):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        return _once(s, dinner, op_id, lambda: orders.delete_line(s, dinner, actor, line_id, version))


@router.post("/api/d/{public_token}/shortlist")
def shortlist(public_token: str, request: Request, body: dict = Body(...)):
    """Personal 'maybe' list. Never touches the bill."""
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        if actor.participant is None:
            raise HTTPException(401, "Join the dinner first.")
        item = s.get(MenuItem, body.get("menu_item_id", ""))
        if item is None or item.dinner_id != dinner.id:
            raise HTTPException(404, "Not on this menu.")
        existing = s.get(Shortlist, (actor.participant.id, item.id))
        if body.get("on") and existing is None:
            s.add(Shortlist(participant_id=actor.participant.id, menu_item_id=item.id))
        elif not body.get("on") and existing is not None:
            s.delete(existing)
    return {"ok": True}


@router.post("/api/d/{public_token}/sent")
def mark_sent(public_token: str, request: Request, body: dict = Body(...)):
    with locked_write() as s:
        dinner, actor = _fresh(s, request, public_token)
        if actor.participant is None:
            raise HTTPException(401, "Join the dinner first.")
        payments.mark_sent(s, dinner, actor.participant, bool(body.get("sent", True)))
    return {"ok": True}


@router.get("/api/d/{public_token}/events")
async def events(ctx: GuestContext = Depends(guest_context)):
    return StreamingResponse(
        broker.stream(dinner_channel(ctx.dinner.id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/api/d/{public_token}/image/{name}")
def image(name: str, ctx: GuestContext = Depends(guest_context), s: Session = Depends(get_session)):
    """Menu/receipt photos, only for people at this dinner."""
    if ctx.participant is None and not ctx.organiser:
        raise HTTPException(401, "Join the dinner first.")
    if not ctx.organiser:
        page = s.exec(select(MenuPage).where(MenuPage.dinner_id == ctx.dinner.id, MenuPage.image_file == name)).first()
        receipts = s.exec(select(Receipt).where(Receipt.dinner_id == ctx.dinner.id)).all()
        in_receipt = any(name in (r.image_files or []) for r in receipts)
        if not in_receipt and (page is None or page.status != "published"):
            raise HTTPException(404, "Not found.")
    path = image_path(ctx.dinner.id, name)
    if path is None:
        raise HTTPException(404, "Not found.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
