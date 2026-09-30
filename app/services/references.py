"""Dinner codes and payment references.

A reference is `<CODE>-<NAME>`, e.g. DIN7-HELEN. It is built from letters and
digits only (plus the one hyphen), upper case, and at most 18 characters —
the lowest common limit among Australian banking apps' reference fields
(the legacy BECS lodgement reference). Anything a bank app might reject or
mangle is removed rather than escaped.

A reference is issued once when someone joins and is never regenerated, even
if they change their display name, because they may already have typed it
into their banking app.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container

from sqlmodel import Session, select

from app.models import Participant, Setting

MAX_REFERENCE_LEN = 18
CODE_PREFIX = "DIN"


def next_dinner_code(session: Session) -> str:
    row = session.get(Setting, "dinner_counter")
    n = int(row.value) + 1 if row else 1
    if row:
        row.value = n
    else:
        row = Setting(key="dinner_counter", value=n)
    session.add(row)
    return f"{CODE_PREFIX}{n}"


def name_token(display_name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", display_name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    token = re.sub(r"[^A-Z0-9]", "", ascii_only.upper())
    return token or "GUEST"


def make_reference(code: str, display_name: str, taken: Container[str]) -> str:
    room = MAX_REFERENCE_LEN - len(code) - 1
    if room < 2:
        raise ValueError(f"dinner code {code!r} leaves no room for a name")
    base = name_token(display_name)[:room]
    candidate = f"{code}-{base}"
    n = 2
    while candidate in taken:
        suffix = str(n)
        candidate = f"{code}-{base[: room - len(suffix)]}{suffix}"
        n += 1
    return candidate


def issue_reference(session: Session, code: str, display_name: str) -> str:
    taken = set(session.exec(select(Participant.reference).where(Participant.reference.startswith(f"{code}-"))).all())
    return make_reference(code, display_name, taken)


def compact(text: str) -> str:
    """Upper-case letters and digits only — how references are compared."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^A-Z0-9]", "", decomposed.upper())


def reference_found(reference: str, *texts: str) -> str | None:
    """How strongly `reference` appears in the transfer's text fields.

    'exact'  — the reference stands on its own, separators optional
               (DIN7-HELEN, din7 helen, DIN7HELEN.)
    'loose'  — only found once every separator is stripped, so it may be the
               start of something longer (DIN7-HELEN G…).
    None     — not present.
    """
    code, _, name = reference.partition("-")
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(code)}[\s\-_./:#]*{re.escape(name)}(?![A-Z0-9])")
    for t in texts:
        if t and pattern.search(unicodedata.normalize("NFKD", t).upper()):
            return "exact"
    target = compact(reference)
    if any(target in compact(t) for t in texts if t):
        return "loose"
    return None
