"""Who is asking.

Organiser: one password (ADMIN_PASSWORD) exchanged for a signed, HttpOnly
cookie. Changing the password signs every device out.

Guests: joining a dinner issues a random session token, stored only in that
guest's cookie; the server keeps its SHA-256. Names are just labels — knowing
someone's name gets you nothing. A guest who loses their cookie is recovered
by the organiser issuing a fresh personal link, never by typing a name.

Cross-site requests: every state-changing API call must carry the
`X-DinnerTab` header. Browsers will not attach custom headers to a
cross-origin request without a CORS preflight, which this app never grants,
so a hostile page cannot drive a signed-in browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.models import Dinner, Participant

ORGANISER_COOKIE = "dt_org"
ORGANISER_MAX_AGE = 90 * 24 * 3600
GUEST_MAX_AGE = 60 * 24 * 3600
CSRF_HEADER = "x-dinnertab"


# ------------------------------------------------------------------ helpers
def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="dinnertab-organiser")


def _password_fingerprint() -> str:
    s = get_settings()
    return hmac.new(s.secret_key.encode(), s.admin_password.encode(), "sha256").hexdigest()[:16]


def _cookie_kwargs() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": get_settings().is_production}


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# -------------------------------------------------------------- rate limits
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault((bucket, key), deque())
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(request: Request, bucket: str, limit: int, window: float) -> None:
    if not limiter.check(bucket, client_ip(request), limit, window):
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")


# ---------------------------------------------------------------- organiser
def password_ok(candidate: str) -> bool:
    expected = get_settings().admin_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def sign_in(response: Response) -> None:
    token = _serializer().dumps({"fp": _password_fingerprint()})
    response.set_cookie(ORGANISER_COOKIE, token, max_age=ORGANISER_MAX_AGE, **_cookie_kwargs())


def sign_out(response: Response) -> None:
    response.delete_cookie(ORGANISER_COOKIE)


def is_organiser(request: Request) -> bool:
    raw = request.cookies.get(ORGANISER_COOKIE)
    if not raw or not get_settings().admin_password:
        return False
    try:
        data = _serializer().loads(raw, max_age=ORGANISER_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(data.get("fp", "")), _password_fingerprint())


def require_organiser(request: Request) -> None:
    if not is_organiser(request):
        raise HTTPException(401, "Organiser sign-in required.")


# ------------------------------------------------------------------- guests
def guest_cookie_name(dinner: Dinner) -> str:
    return f"dt_{dinner.id[:12]}"


def issue_guest_session(response: Response, dinner: Dinner, participant: Participant) -> str:
    token = secrets.token_urlsafe(32)
    participant.session_hash = sha256(token)
    response.set_cookie(guest_cookie_name(dinner), token, max_age=GUEST_MAX_AGE, **_cookie_kwargs())
    return token


def dinner_by_token(session: Session, public_token: str) -> Dinner:
    dinner = session.exec(select(Dinner).where(Dinner.public_token == public_token)).first()
    if dinner is None or dinner.archived:
        raise HTTPException(404, "This dinner link isn't valid.")
    return dinner


def participant_from_request(session: Session, request: Request, dinner: Dinner) -> Participant | None:
    token = request.cookies.get(guest_cookie_name(dinner)) or request.headers.get("x-guest-token")
    if not token:
        return None
    p = session.exec(
        select(Participant).where(Participant.session_hash == sha256(token), Participant.dinner_id == dinner.id)
    ).first()
    if p is None or p.removed:
        return None
    return p


class GuestContext:
    def __init__(self, dinner: Dinner, participant: Participant | None, organiser: bool):
        self.dinner = dinner
        self.participant = participant
        self.organiser = organiser

    def require_participant(self) -> Participant:
        if self.participant is None:
            raise HTTPException(401, "Join the dinner first.")
        return self.participant


def guest_context(public_token: str, request: Request, session: Session = Depends(get_session)) -> GuestContext:
    dinner = dinner_by_token(session, public_token)
    return GuestContext(dinner, participant_from_request(session, request, dinner), is_organiser(request))


# ------------------------------------------------------------ secrets at rest
def _fernet() -> Fernet:
    digest = hashlib.sha256(("dinnertab-at-rest:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None
