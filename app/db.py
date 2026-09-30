"""Engine, sessions and schema versioning.

SQLite in WAL mode, one application process. Every write that reads-then-
writes (version checks, allocation, payment matching) runs under
`write_lock`, which serialises them within the process: two people tapping
the same dish at the same moment are handled one after the other, and the
second sees the first's result. This is why the app must run as a single
worker — see the README.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.config import get_settings

SCHEMA_VERSION = 1

write_lock = threading.RLock()
_engine: Engine | None = None


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            path = url.removeprefix("sqlite:///")
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args={"check_same_thread": False})
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _sqlite_pragmas)
    return _engine


def reset_engine() -> None:
    """Used by tests to point at a fresh database."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        current = conn.exec_driver_sql("PRAGMA user_version").scalar() or 0
        # Future schema changes go here, gated on `current`, then bump
        # SCHEMA_VERSION. create_all() already built version 1.
        if current < SCHEMA_VERSION:
            conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")


def get_session() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


@contextmanager
def locked_write() -> Iterator[Session]:
    """A session for a read-modify-write unit, serialised with all others."""
    with write_lock, Session(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
