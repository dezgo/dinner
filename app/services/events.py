"""Live updates.

Every change to a dinner bumps its `revision`. After the change commits, a
tiny notice — just the dinner id and the new revision, never any data — is
pushed to every open page for that dinner over Server-Sent Events. Pages then
re-fetch the state they are entitled to see. A page that was offline simply
re-fetches on reconnect, so a missed notice costs nothing.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlmodel import Session

from app.models import AuditEntry, Dinner

ORGANISER_CHANNEL = "organiser"


class Broker:
    def __init__(self) -> None:
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()

    def publish(self, channel: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(channel, ()))
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except RuntimeError:
                pass  # loop closed; the subscriber is going away

    async def stream(self, channel: str, heartbeat: float = 20.0) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        entry = (loop, queue)
        with self._lock:
            self._subs.setdefault(channel, set()).add(entry)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    yield f"data: {json.dumps(payload)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with self._lock:
                self._subs.get(channel, set()).discard(entry)

    def subscriber_count(self, channel: str) -> int:
        with self._lock:
            return len(self._subs.get(channel, ()))


broker = Broker()


def dinner_channel(dinner_id: str) -> str:
    return f"dinner:{dinner_id}"


def touch(session: Session, dinner: Dinner, kind: str = "changed") -> None:
    """Mark the dinner changed; subscribers are told once the commit lands."""
    dinner.revision += 1
    session.add(dinner)
    pending = session.info.setdefault("publish", {})
    pending[dinner.id] = {"dinner": dinner.id, "revision": dinner.revision, "kind": kind}


def audit(session: Session, dinner_id: str | None, actor: str, message: str) -> None:
    session.add(AuditEntry(dinner_id=dinner_id, actor=actor, message=message[:500]))


@event.listens_for(Session, "after_commit")
def _publish_after_commit(session: Session) -> None:
    pending = session.info.pop("publish", None)
    if not pending:
        return
    for dinner_id, payload in pending.items():
        broker.publish(dinner_channel(dinner_id), payload)
        broker.publish(ORGANISER_CHANNEL, payload)


@event.listens_for(Session, "after_rollback")
def _discard_after_rollback(session: Session) -> None:
    session.info.pop("publish", None)
