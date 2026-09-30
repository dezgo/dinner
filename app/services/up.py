"""Up Bank integration (https://developer.up.com.au, API v1).

Verified against Up's published OpenAPI spec:

* Auth is a personal access token as `Authorization: Bearer …`. It lives only
  in the server's environment (UP_API_TOKEN).
* Webhook events carry only an event type and a *link* to the transaction;
  the transaction itself must be fetched with GET /transactions/{id}.
* Each event has an `id` that "will remain constant across delivery retries" —
  used here to make processing idempotent.
* The `X-Up-Authenticity-Signature` header is the hex SHA-256 HMAC of the raw
  body using the webhook's `secretKey`, which Up returns only once, at
  creation. It is stored encrypted in the database.
* Up waits 30 s for a 200 and retries with backoff otherwise, so the handler
  stores the event and answers immediately; the work happens afterwards.
* Event types: PING, TRANSACTION_CREATED, TRANSACTION_SETTLED,
  TRANSACTION_DELETED (held transactions only; no link is provided).
  Up notes SETTLED may occasionally arrive as DELETED + CREATED instead.
* Transaction fields used: amount.valueInBaseUnits (cents, negative for
  debits), createdAt, status (HELD/SETTLED), description, message, rawText,
  relationships.account.data.id. Up does not document which of the text
  fields carries a PayID payer's reference or name.
* Rate limiting is signalled by HTTP 429; the remaining allowance is in
  X-RateLimit-Remaining.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.config import get_settings
from app.db import locked_write, session_scope
from app.models import Dinner, Setting, WebhookEvent, utcnow
from app.security import decrypt, encrypt
from app.services import jobs, payments

logger = logging.getLogger(__name__)


class UpError(Exception):
    pass


# --------------------------------------------------------------- settings
def get_setting(s: Session, key: str, default=None):
    row = s.get(Setting, key)
    return row.value if row else default


def put_setting(s: Session, key: str, value) -> None:
    row = s.get(Setting, key) or Setting(key=key)
    row.value = value
    s.add(row)


def webhook_secret(s: Session) -> str | None:
    enc = get_setting(s, "up_webhook_secret")
    return decrypt(enc) if enc else None


# ------------------------------------------------------------------ client
class UpClient:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None):
        settings = get_settings()
        token = token or settings.up_api_token
        if not token:
            raise UpError("Up isn't connected: set UP_API_TOKEN on the server.")
        self._http = httpx.Client(
            base_url=settings.up_api_base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
            transport=transport,
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self._http.get(url, params=params)
        return self._check(r)

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code == 401:
            raise UpError("Up rejected the API token (expired or revoked?).")
        if r.status_code == 429:
            raise UpError("Up is rate limiting requests; will retry later.")
        if r.status_code >= 400:
            raise UpError(f"Up returned HTTP {r.status_code}.")
        return r.json() if r.content else {}

    def ping(self) -> bool:
        self._get("/util/ping")
        return True

    def accounts(self) -> list[dict]:
        out, url, params = [], "/accounts", {"page[size]": 100}
        while url:
            data = self._get(url, params)
            params = None
            for a in data.get("data", []):
                attrs = a["attributes"]
                # Balance deliberately not returned: nothing here needs it.
                out.append(
                    {
                        "id": a["id"],
                        "name": attrs["displayName"],
                        "type": attrs["accountType"],
                        "ownership": attrs["ownershipType"],
                    }
                )
            url = (data.get("links") or {}).get("next")
        return out

    def transaction(self, tx_id: str) -> dict | None:
        r = self._http.get(f"/transactions/{tx_id}")
        if r.status_code == 404:
            return None
        return normalise(self._check(r)["data"])

    def recent(self, account_id: str, since: datetime) -> list[dict]:
        out = []
        url = f"/accounts/{account_id}/transactions"
        params = {"page[size]": 100, "filter[since]": since.isoformat()}
        while url:
            data = self._get(url, params)
            params = None  # the `next` link already carries them
            out += [normalise(t) for t in data.get("data", [])]
            url = (data.get("links") or {}).get("next")
        return out

    def create_webhook(self, url: str) -> tuple[str, str]:
        r = self._http.post(
            "/webhooks",
            json={"data": {"attributes": {"url": url, "description": "Dinner Tab payments"}}},
        )
        data = self._check(r)["data"]
        return data["id"], data["attributes"]["secretKey"]

    def delete_webhook(self, webhook_id: str) -> None:
        r = self._http.delete(f"/webhooks/{webhook_id}")
        if r.status_code not in (204, 404):
            self._check(r)

    def ping_webhook(self, webhook_id: str) -> None:
        self._check(self._http.post(f"/webhooks/{webhook_id}/ping"))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def normalise(t: dict) -> dict:
    attrs = t["attributes"]
    rel = t.get("relationships") or {}
    return {
        "id": t["id"],
        "account_id": ((rel.get("account") or {}).get("data") or {}).get("id", ""),
        "status": attrs.get("status", "SETTLED"),
        "amount_cents": int(attrs["amount"]["valueInBaseUnits"]),
        "description": attrs.get("description") or "",
        "message": attrs.get("message") or "",
        "raw_text": attrs.get("rawText") or "",
        "created_at": parse_time(attrs["createdAt"]),
    }


# ---------------------------------------------------------------- webhooks
def signature_valid(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def record_event(payload: dict) -> tuple[str, bool]:
    """Store a verified event. Returns (event id, needs processing)."""
    data = payload["data"]
    event_id = data["id"]
    etype = data["attributes"]["eventType"]
    tx = ((data.get("relationships") or {}).get("transaction") or {}).get("data") or {}
    with locked_write() as s:
        existing = s.get(WebhookEvent, event_id)
        if existing is not None:
            # A retry of something we already have. Only re-run it if the
            # earlier attempt never finished.
            return event_id, existing.processed_at is None
        s.add(WebhookEvent(id=event_id, event_type=etype, transaction_id=tx.get("id")))
    return event_id, True


def process_event(event_id: str, client: UpClient | None = None) -> None:
    with session_scope() as s:
        ev = s.get(WebhookEvent, event_id)
        if ev is None or ev.processed_at is not None:
            return
        account_id = get_setting(s, "up_account_id")
        etype, tx_id = ev.event_type, ev.transaction_id
    error = None
    try:
        if etype in ("TRANSACTION_CREATED", "TRANSACTION_SETTLED") and tx_id and account_id:
            data = (client or UpClient()).transaction(tx_id)
            if data and data["account_id"] == account_id and data["amount_cents"] > 0:
                with locked_write() as s:
                    payments.ingest(s, data)
        elif etype == "TRANSACTION_DELETED" and tx_id:
            with locked_write() as s:
                payments.mark_deleted(s, tx_id)
    except UpError as e:
        error = str(e)
        logger.warning("webhook %s: %s", event_id, e)
    with locked_write() as s:
        ev = s.get(WebhookEvent, event_id)
        if error:
            # Left unprocessed; the periodic sync will pick the transaction up.
            ev.error = error
        else:
            ev.processed_at = utcnow()
            ev.error = None
        s.add(ev)


def queue_event(event_id: str) -> None:
    jobs.submit(process_event, event_id)


# -------------------------------------------------------------------- sync
def sync_window_start(s: Session) -> datetime | None:
    now = utcnow()
    starts = [
        d.instructions_issued_at
        for d in s.exec(
            select(Dinner).where(Dinner.instructions_issued_at != None, Dinner.is_demo == False)  # noqa: E711,E712
        ).all()
        if d.payment_window_ends_at is None or d.payment_window_ends_at >= now
    ]
    return min(starts) - timedelta(hours=1) if starts else None


def sync(client: UpClient | None = None) -> dict:
    """Re-read recent credits so a missed webhook can't lose a payment."""
    with session_scope() as s:
        account_id = get_setting(s, "up_account_id")
        since = sync_window_start(s)
    if not account_id:
        return {"skipped": "No receiving account chosen."}
    if since is None:
        return {"skipped": "No payment windows are open."}
    try:
        txs = (client or UpClient()).recent(account_id, since)
    except UpError as e:
        with locked_write() as s:
            put_setting(s, "up_last_error", {"at": utcnow().isoformat(), "message": str(e)})
        raise
    seen = 0
    for data in txs:
        if data["amount_cents"] <= 0:
            continue
        with locked_write() as s:
            if payments.ingest(s, data) is not None:
                seen += 1
    # Retry any webhook events whose processing failed earlier.
    with session_scope() as s:
        stuck = [e.id for e in s.exec(select(WebhookEvent).where(WebhookEvent.processed_at == None)).all()]  # noqa: E711
    for eid in stuck:
        process_event(eid, client)
    with locked_write() as s:
        put_setting(s, "up_last_sync", {"at": utcnow().isoformat(), "credits": seen})
        put_setting(s, "up_last_error", None)
    return {"credits_checked": seen}


def register_webhook(public_base: str, client: UpClient | None = None) -> dict:
    url = public_base.rstrip("/") + "/webhooks/up"
    if not url.startswith("https://"):
        raise UpError("The webhook needs a public HTTPS address (set PUBLIC_BASE_URL).")
    if len(url) > 300:
        raise UpError("Up limits webhook URLs to 300 characters.")
    client = client or UpClient()
    with session_scope() as s:
        old = get_setting(s, "up_webhook_id")
    if old:
        client.delete_webhook(old)
    webhook_id, secret = client.create_webhook(url)
    with locked_write() as s:
        put_setting(s, "up_webhook_id", webhook_id)
        put_setting(s, "up_webhook_secret", encrypt(secret))
        put_setting(s, "up_webhook_url", url)
    client.ping_webhook(webhook_id)
    return {"id": webhook_id, "url": url}
