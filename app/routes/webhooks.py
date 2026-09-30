"""Up Bank webhook receiver: POST /webhooks/up

Verifies the signature over the raw body, records the event by its id (so a
redelivery is recognised), answers 200 straight away, and processes after.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.db import session_scope
from app.services import up

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/up")
async def up_webhook(request: Request):
    raw = await request.body()
    with session_scope() as s:
        secret = up.webhook_secret(s)
    if not up.signature_valid(raw, request.headers.get("x-up-authenticity-signature"), secret):
        logger.warning("rejected Up webhook with a bad or missing signature")
        raise HTTPException(401, "Bad signature.")
    try:
        payload = json.loads(raw)
        event_id, needs_work = up.record_event(payload)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, "Unrecognised event.") from e
    if needs_work:
        up.queue_event(event_id)
    return {"ok": True}
