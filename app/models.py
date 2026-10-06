"""Database tables.

Money is always integer cents. Timestamps are timezone-aware UTC.

Rows that people edit concurrently (menu items, order lines, the dinner
itself) carry a `version` that every write must quote back; a stale version
is rejected with 409 rather than silently overwriting someone else's change.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, Index, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid4().hex


def new_token() -> str:
    # 128+ bits; unguessable dinner links and participant sessions.
    return secrets.token_urlsafe(24)


def utcnow() -> datetime:
    return datetime.now(UTC)


def json_col(default: Any = None) -> Any:
    factory = (lambda: list(default)) if isinstance(default, list) else (lambda: default)
    return Field(default_factory=factory, sa_column=Column(JSON))


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))


class Restaurant(SQLModel, table=True):
    """A place you go back to. Keeps its most recent menu so the next dinner
    there starts with it already loaded. Each dinner still gets its own copy,
    so a later change never alters an old bill."""

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    name_key: str = Field(index=True, unique=True)  # lower-cased, spaces collapsed
    # {"pages": [...], "categories": [...], "items": [...]} — see services/restaurants.py
    menu: dict = Field(default_factory=dict, sa_column=Column(JSON))
    menu_dinner_id: str | None = None  # the dinner the saved menu came from
    menu_updated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Dinner(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g. DIN7 — prefix of every reference
    public_token: str = Field(default_factory=new_token, index=True, unique=True)
    restaurant_name: str = ""
    restaurant_id: str | None = Field(default=None, index=True)
    table_label: str = ""  # the restaurant's table number or name, e.g. "12" or "Courtyard 3"
    is_demo: bool = False
    status: str = "open"  # open | finalised
    revision: int = 0  # bumped on every change; clients re-fetch when it moves
    version: int = 1  # for bill-level edits (total, finalisation)

    # The restaurant's own figure, from a receipt or quoted verbally.
    bill_total_cents: int | None = None
    bill_total_source: str | None = None  # receipt | quoted

    organiser_participant_id: str | None = None

    finalised_at: datetime | None = None
    finalise_count: int = 0
    # When payment instructions were first released. Kept across reopening so
    # the payment window still covers transfers made before a reopen.
    instructions_issued_at: datetime | None = None
    payment_window_ends_at: datetime | None = None
    # Everyone's share as locked at the most recent finalisation.
    final_shares: dict = Field(default_factory=dict, sa_column=Column(JSON))
    archived: bool = False

    created_at: datetime = Field(default_factory=utcnow)


class Participant(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    display_name: str
    # Issued once, never changed, even if the display name is edited.
    reference: str = Field(index=True, unique=True)
    # SHA-256 of the participant's session token; the token itself is only
    # ever in that person's cookie.
    session_hash: str | None = Field(default=None, index=True)
    # One-time personal link the organiser can issue if a guest loses their
    # session (new phone, cleared cookies). Hash only; expires.
    recovery_hash: str | None = Field(default=None, index=True)
    recovery_expires_at: datetime | None = None
    is_organiser: bool = False
    sort: int = 0
    removed: bool = False
    # A guest saying "I've sent it". Not proof of anything.
    marked_sent_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class MenuPage(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    kind: str = "menu"  # menu | specials | manual
    image_file: str | None = None
    # processing -> review -> published; or failed / manual
    status: str = "processing"
    error: str | None = None
    legend: list = json_col([])  # [{symbol, meaning}] as printed on the menu
    flags: list = json_col([])  # page-level notes, e.g. "bottom of page cut off"
    from_saved: bool = False  # copied from the restaurant's saved menu, not scanned tonight
    sort: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class MenuCategory(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    page_id: str | None = Field(default=None, index=True)
    name: str
    note: str = ""
    extras: list = json_col([])  # [{label, price_cents}] offered to every dish here
    sort: int = 0


class MenuItem(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    page_id: str | None = Field(default=None, index=True)
    category_id: str | None = Field(default=None, index=True)
    name: str
    description: str = ""
    price_cents: int | None = None
    price_text: str = ""  # exactly as printed, for checking
    variants: list = json_col([])  # [{label, price_cents}]
    extras: list = json_col([])  # [{label, price_cents}]
    labels: list = json_col([])  # dietary symbols exactly as printed, e.g. ["V", "GF"]
    diet: list = json_col([])  # normalised explicit tags: vegetarian, vegan, gluten_free…
    # vegan: {"status": "marked"|"on_request"|"possible"|None, "note": str}
    # "possible" is the model's reading of the description, never the menu's claim.
    vegan: dict = Field(default_factory=dict, sa_column=Column(JSON))
    flags: list = json_col([])  # [{field, message}] — uncertain text, missing price…
    unavailable: bool = False
    is_special: bool = False
    source: str = "extracted"  # extracted | manual
    # Copied from the restaurant's saved menu and not (yet) found on a fresh scan.
    from_saved: bool = False
    # What a fresh scan changed compared with the saved menu, e.g. "Price was $24.00", "New".
    change_note: str = ""
    sort: int = 0
    version: int = 1


class OrderLine(SQLModel, table=True):
    """One thing on the bill: a dish, drink, surcharge, discount or adjustment.

    Menu-first, receipt-first and manual bills all end up as these rows, so
    allocation, finalisation and payment are one code path.
    """

    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    kind: str = "item"  # item | surcharge | discount | adjustment
    source: str = "menu"  # menu | manual | receipt
    menu_item_id: str | None = None
    name: str
    variant_label: str = ""
    extras: list = json_col([])  # [{label, price_cents}] per unit
    unit_price_cents: int = 0  # includes the chosen variant, excludes extras
    quantity: int = 1
    # Set when the line total is taken from the restaurant's figure (receipt
    # reconciliation, or a manually entered amount).
    total_override_cents: int | None = None
    note: str = ""
    # equal | shares | units (per-unit claims on a quantity line) |
    # proportional (surcharges/discounts: by each person's item subtotal)
    split_mode: str = "equal"
    shared: bool = False
    created_by: str | None = None
    # When a receipt exists, lines the receipt doesn't show must be looked at.
    not_on_receipt_ok: bool = False
    deleted: bool = False
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Allocation(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("line_id", "participant_id"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    line_id: str = Field(index=True, foreign_key="orderline.id")
    participant_id: str = Field(index=True, foreign_key="participant.id")
    # equal: 1; shares: relative weight; units: units claimed
    weight: int = 1
    created_at: datetime = Field(default_factory=utcnow)


class ClientOp(SQLModel, table=True):
    """Idempotency record: a retried request returns the original result."""

    op_id: str = Field(primary_key=True)
    dinner_id: str = Field(index=True)
    response: Any = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Shortlist(SQLModel, table=True):
    participant_id: str = Field(primary_key=True, foreign_key="participant.id")
    menu_item_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=utcnow)


class Receipt(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    status: str = "processing"  # processing | review | failed | discarded
    image_files: list = json_col([])
    total_cents: int | None = None
    subtotal_cents: int | None = None
    gst_cents: int | None = None
    gst_included: bool | None = None
    flags: list = json_col([])
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ReceiptLine(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    receipt_id: str = Field(index=True, foreign_key="receipt.id")
    dinner_id: str = Field(index=True)
    sort: int = 0
    description: str
    quantity: int = 1
    unit_price_cents: int | None = None
    line_total_cents: int | None = None
    # item | surcharge | discount | tip | tax_info | subtotal | total | payment | other
    kind: str = "item"
    flags: list = json_col([])
    # pending -> matched (to existing lines) | added (as a new line) | ignored
    state: str = "pending"
    matched_line_ids: list = json_col([])
    suggestion: dict = Field(default_factory=dict, sa_column=Column(JSON))
    added_line_id: str | None = None


class BankTransaction(SQLModel, table=True):
    """An incoming credit to the receiving account, seen during a payment window.

    Only credits to the one designated account are stored — never debits,
    never balances, never other accounts. Guests never see these rows.
    """

    id: str = Field(primary_key=True)  # Up's transaction id (or sim-… for demos)
    account_id: str = ""
    status: str = "SETTLED"  # HELD | SETTLED | DELETED
    amount_cents: int
    description: str = ""
    message: str = ""
    raw_text: str = ""
    created_at: datetime
    first_seen_at: datetime = Field(default_factory=utcnow)
    # unmatched | auto | review | confirmed | ignored
    match_state: str = "unmatched"
    match_note: str = ""
    candidates: list = json_col([])
    is_simulated: bool = False


class Payment(SQLModel, table=True):
    """Money actually received from a guest. Never deleted; undo is recorded."""

    __table_args__ = (
        # One bank transfer can confirm at most one reimbursement.
        Index(
            "ux_payment_bank_tx_live",
            "bank_transaction_id",
            unique=True,
            sqlite_where=text("undone_at IS NULL AND bank_transaction_id IS NOT NULL"),
        ),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    dinner_id: str = Field(index=True, foreign_key="dinner.id")
    participant_id: str = Field(index=True, foreign_key="participant.id")
    amount_cents: int
    source: str = "manual"  # up_auto | up_review | manual | simulated
    bank_transaction_id: str | None = None
    match_reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    undone_at: datetime | None = None
    undo_reason: str = ""


class WebhookEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)  # Up's event id: constant across retries
    event_type: str
    transaction_id: str | None = None
    received_at: datetime = Field(default_factory=utcnow)
    processed_at: datetime | None = None
    error: str | None = None


class AuditEntry(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    dinner_id: str | None = Field(default=None, index=True)
    at: datetime = Field(default_factory=utcnow)
    actor: str = ""
    message: str
