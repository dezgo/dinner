from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.paths import TEMPLATES_DIR
from app.services.money import fmt

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__
templates.env.filters["money"] = fmt


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"request": request, **(context or {})}
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response
