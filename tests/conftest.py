"""Test setup: a fresh database and upload folder per test, no network.

Photo reading uses the canned extractor, Up is a fake transport, and
background jobs run inline so results are immediate.
"""

from __future__ import annotations

import os
import tempfile

os.environ.update(
    {
        "ENV": "test",
        "SECRET_KEY": "test-secret-key",
        "ADMIN_PASSWORD": "organiser-pw",
        "ANTHROPIC_API_KEY": "",
        "UP_API_TOKEN": "",
        "DEMO_TOOLS": "true",
        "TRUSTED_HOSTS": "*",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import init_db, locked_write, reset_engine  # noqa: E402
from app.security import limiter  # noqa: E402
from app.services import jobs  # noqa: E402

H = {"X-DinnerTab": "1"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    reset_engine()
    init_db()
    limiter.reset()
    jobs.run_inline = True
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def new_client():
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def organiser(client):
    r = client.post("/api/login", json={"password": "organiser-pw"}, headers=H)
    assert r.status_code == 200
    return client


class Dinner:
    """Convenience wrapper: one organiser client plus guest clients."""

    def __init__(self, org: TestClient, dinner_id: str):
        self.org = org
        self.id = dinner_id
        self.token = self.state()["public_token"]
        self.base = f"/api/d/{self.token}"

    def state(self) -> dict:
        r = self.org.get(f"/api/o/d/{self.id}/state")
        assert r.status_code == 200, r.text
        return r.json()

    def guest(self, name: str) -> TestClient:
        c = new_client()
        r = c.post(f"{self.base}/join", json={"name": name}, headers=H)
        assert r.status_code == 200, r.text
        c.pid = r.json()["participant_id"]
        c.reference = r.json()["reference"]
        return c

    def gstate(self, c: TestClient) -> dict:
        r = c.get(f"{self.base}/state")
        assert r.status_code == 200, r.text
        return r.json()

    def add_item(self, name: str, price: int | None, **kw) -> str:
        r = self.org.post(f"/api/o/d/{self.id}/items", json={"name": name, "price_cents": price, **kw}, headers=H)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def record(self, c: TestClient, **body) -> dict:
        r = c.post(f"{self.base}/lines", json=body, headers=H)
        assert r.status_code == 200, r.text
        return r.json()

    def line(self, line_id: str) -> dict:
        return next(ln for ln in self.state()["lines"] if ln["id"] == line_id)


@pytest.fixture
def dinner(organiser):
    r = organiser.post("/api/o/dinners", json={"restaurant_name": "Test Bistro", "my_name": "Derek"}, headers=H)
    assert r.status_code == 200
    return Dinner(organiser, r.json()["id"])


def set_profile():
    from app.services.up import put_setting

    with locked_write() as s:
        put_setting(
            s,
            "payment_profile",
            {
                "payid": "0400000000",
                "payid_type": "phone",
                "recipient_name": "Derek W",
                "bsb": "",
                "account_number": "",
            },
        )


__all__ = ["H", "Dinner", "new_client", "set_profile", "tempfile"]
