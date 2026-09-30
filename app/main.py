"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.config import get_settings
from app.db import init_db, session_scope
from app.models import MenuPage, Receipt
from app.paths import STATIC_DIR
from app.routes import guest, organiser, webhooks
from app.security import CSRF_HEADER
from app.services import menu, reconcile, up
from app.templating import render

logger = logging.getLogger("dinnertab")

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; font-src 'self'; manifest-src 'self'; "
    "worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _resume_unfinished_work() -> None:
    """Photos that were mid-read when the server stopped are read again."""
    with session_scope() as s:
        pages = s.exec(select(MenuPage.id).where(MenuPage.status == "processing")).all()
        receipts = s.exec(select(Receipt.id).where(Receipt.status == "processing")).all()
    for pid in pages:
        menu.queue_page(pid)
    for rid in receipts:
        reconcile.queue_receipt(rid)


async def _up_sync_loop() -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(max(settings.up_poll_minutes, 1) * 60)
        if not settings.up_enabled:
            continue
        try:
            result = await asyncio.to_thread(up.sync)
            if result.get("credits_checked"):
                logger.info("up sync: %s", result)
        except up.UpError as e:
            logger.warning("up sync failed: %s", e)
        except Exception:
            logger.exception("up sync crashed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=get_settings().log_level)
    init_db()
    from app.services.demo import register_samples

    register_samples()
    _resume_unfinished_work()
    task = asyncio.create_task(_up_sync_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Dinner Tab", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/api/"):
            # Never let a browser or the service worker keep API responses.
            response.headers["Cache-Control"] = "no-store"
        return response

    hosts = settings.trusted_host_list
    if hosts and hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*hosts, "127.0.0.1", "localhost"])
    if settings.trust_proxy_headers:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/webhooks/")) or exc.status_code not in (404, 401):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return render(request, "error.html", {"message": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Some details were missing or invalid."}, status_code=422)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        # Served from the root so it may control organiser pages.
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with session_scope() as s:
            s.exec(select(MenuPage.id).limit(1)).all()
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def home(request: Request):
        return render(request, "home.html")

    app.include_router(organiser.router)
    app.include_router(organiser.api)
    app.include_router(guest.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
