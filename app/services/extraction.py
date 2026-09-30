"""Reading menu and receipt photos.

Claude reads each photo into a strict JSON schema. The instructions are
transcription, not interpretation: copy what is printed, leave blanks blank,
and flag anything unclear. Then this module double-checks the result in code
— prices against the printed text, "vegan" claims against the printed labels,
receipt lines against the receipt total — and adds flags where they disagree.

When no Anthropic key is configured, or for demo dinners, a canned extractor
is used instead, so the whole flow works without spending anything.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.money import parse_price

logger = logging.getLogger(__name__)

DietTag = Literal["vegetarian", "vegan", "gluten_free", "dairy_free", "nut_free", "contains_nuts", "halal", "spicy"]


class PriceOption(BaseModel):
    label: str = Field(description="Exactly as printed, e.g. 'Glass', '500ml', 'Add prawns'.")
    price_text: str = Field(description="The price exactly as printed, or '' if none is shown.")
    price_cents: int | None = Field(description="The printed price in cents, or null if none.")


class Flag(BaseModel):
    field: Literal["name", "description", "price", "variants", "extras", "dietary", "other"]
    message: str = Field(description="What is unclear, in a few plain words.")


class LegendEntry(BaseModel):
    symbol: str
    meaning: str


class ExtractedItem(BaseModel):
    name: str
    description: str = Field(description="Verbatim from the menu; '' if the menu gives none.")
    price_text: str = Field(description="Verbatim price as printed, '' if none.")
    price_cents: int | None
    variants: list[PriceOption] = Field(description="Sizes/versions with their own price.")
    extras: list[PriceOption] = Field(description="Paid or free add-ons printed for this dish.")
    dietary_labels: list[str] = Field(description="Dietary symbols exactly as printed, e.g. 'V'.")
    explicit_diet: list[DietTag] = Field(
        description="Only what the menu itself states via labels, the legend or explicit words."
    )
    vegan_on_request: str = Field(
        description=(
            "If the menu explicitly says this dish can be made vegan, the stated "
            "modification verbatim (e.g. 'vegan without feta'); otherwise ''."
        )
    )
    possibly_vegan: bool = Field(
        description=(
            "Your own inference that the printed description contains no animal products. "
            "False if the menu already marks it vegan or if unsure."
        )
    )
    possibly_vegan_reason: str
    flags: list[Flag]


class ExtractedCategory(BaseModel):
    name: str
    note: str = Field(description="Any note printed under the heading, verbatim; '' if none.")
    extras: list[PriceOption] = Field(description="Add-ons offered for every dish in this section.")
    items: list[ExtractedItem]


class MenuExtraction(BaseModel):
    categories: list[ExtractedCategory]
    legend: list[LegendEntry]
    page_notes: list[str] = Field(description="Problems with the photo itself, e.g. glare.")


class ExtractedReceiptLine(BaseModel):
    description: str = Field(description="Verbatim, including abbreviations.")
    quantity: int
    unit_price_cents: int | None
    line_total_cents: int | None = Field(description="Negative for discounts.")
    kind: Literal["item", "surcharge", "discount", "tip", "tax_info", "subtotal", "total", "payment", "other"]
    uncertain: bool
    note: str = Field(description="Why it is uncertain, or ''.")


class ReceiptExtraction(BaseModel):
    lines: list[ExtractedReceiptLine]
    subtotal_cents: int | None
    total_cents: int | None
    gst_cents: int | None
    gst_included: bool | None = Field(description="True if the receipt says prices/total include GST.")
    notes: list[str]


MENU_INSTRUCTIONS = """\
You are transcribing a photo of a restaurant menu so diners can read it on their phones.
Transcribe; do not interpret or improve.

Rules:
- Copy dish names and descriptions exactly as printed. Never invent, expand or summarise a
  description, and never add ingredients. If there is no description, use "".
- Prices: copy the printed text into price_text and give cents in price_cents. If no price is
  printed for a dish, price_cents is null and add a flag. Never guess a price.
- If it is unclear which price belongs to which dish or size (e.g. prices in a column that
  may not line up), still record your best reading but add a 'price' flag saying so.
- Sizes or versions with their own prices (glass/bottle, small/large, 6/12 pieces) go in
  variants. If a dish has variants, price_cents is null unless a separate base price is printed.
- Add-ons ("add prawns +$6", "extra rice 4") go in extras on the dish, or on the category when
  printed for the whole section.
- Dietary: dietary_labels are the symbols exactly as printed. explicit_diet only includes a
  tag when the menu itself states it (via a symbol explained in the legend, or explicit words
  like "vegan"). Vegetarian is NOT vegan: never tag vegan because a dish is vegetarian.
- vegan_on_request: only when the menu explicitly says the dish can be made vegan, with the
  modification as printed.
- possibly_vegan: your own judgement from the printed description only, and only when nothing
  in it is an animal product. It is shown to diners as "possibly vegan — check with staff".
- Copy the legend (symbol meanings) if one is printed.
- Flag unreadable or partly obscured text rather than guessing. Page-level problems (glare,
  cut-off edges, blur) go in page_notes.
- Keep categories and dishes in the order printed. If there are no headings, use one category
  named "Menu".
"""

RECEIPT_INSTRUCTIONS = """\
You are transcribing a photo of an itemised restaurant receipt so a group can split it.
Transcribe; do not interpret.

Rules:
- One entry per printed line, in order, descriptions verbatim including abbreviations.
- quantity: the printed quantity, or 1 if none is printed.
- line_total_cents: the amount printed for the line. Discounts are negative.
- kind: item (food/drink), surcharge (card/weekend/public holiday/service), discount, tip,
  tax_info (a GST line that is informational because prices include GST), subtotal, total,
  payment (cash/card tendered, change), or other.
- Australian receipts usually show prices including GST with a line like "GST included". Mark
  such a GST line as tax_info and set gst_included true. Only use surcharge for GST if the
  receipt clearly adds GST on top of the subtotal.
- total_cents: the final amount due as printed. subtotal_cents if printed.
- If a number is unreadable or you are unsure, set uncertain true and explain in note. Never
  guess an amount.
"""


class Extractor(Protocol):
    def menu(self, image: bytes, media_type: str) -> MenuExtraction: ...

    def receipt(self, images: list[tuple[bytes, str]]) -> ReceiptExtraction: ...


class ExtractionError(Exception):
    pass


def _image_block(data: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode(),
        },
    }


class ClaudeExtractor:
    def __init__(self) -> None:
        import anthropic

        s = get_settings()
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=3)
        self._model = s.extraction_model
        self._effort = s.extraction_effort

    def _run(self, content: list[dict], schema: type[BaseModel], system: str):
        a = self._anthropic
        try:
            # "default" server-side fallbacks: if a safety classifier ever
            # declines a photo, the same request is retried on a fallback model
            # inside this call rather than just failing.
            with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=64000,
                system=system,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self._effort},
                output_format=schema,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = stream.get_final_message()
        except a.AuthenticationError as e:
            raise ExtractionError("The Anthropic API key was rejected.") from e
        except a.RateLimitError as e:
            raise ExtractionError("Photo reading is rate limited; try again shortly.") from e
        except a.APIStatusError as e:
            raise ExtractionError(f"Photo reading failed ({e.status_code}).") from e
        except a.APIConnectionError as e:
            raise ExtractionError("Couldn't reach the photo-reading service.") from e
        if message.stop_reason == "refusal":
            raise ExtractionError("The photo couldn't be read. Enter these items manually.")
        if message.stop_reason == "max_tokens":
            raise ExtractionError("This page is too long to read in one go; split the photo.")
        parsed = message.parsed_output
        if parsed is None:
            raise ExtractionError("The photo-reading result was incomplete.")
        logger.info(
            "extraction ok model=%s in=%s out=%s req=%s",
            message.model,
            message.usage.input_tokens,
            message.usage.output_tokens,
            getattr(message, "_request_id", ""),
        )
        return parsed

    def menu(self, image: bytes, media_type: str) -> MenuExtraction:
        return self._run(
            [_image_block(image, media_type), {"type": "text", "text": "Transcribe this menu page."}],
            MenuExtraction,
            MENU_INSTRUCTIONS,
        )

    def receipt(self, images: list[tuple[bytes, str]]) -> ReceiptExtraction:
        content = [_image_block(d, m) for d, m in images]
        content.append({"type": "text", "text": "Transcribe this receipt."})
        return self._run(content, ReceiptExtraction, RECEIPT_INSTRUCTIONS)


class CannedExtractor:
    """Returns pre-registered results keyed by image hash (demo and tests)."""

    menus: dict[str, MenuExtraction] = {}
    receipts: dict[str, ReceiptExtraction] = {}

    @classmethod
    def register_menu(cls, image: bytes, result: MenuExtraction) -> None:
        cls.menus[hashlib.sha256(image).hexdigest()] = result

    @classmethod
    def register_receipt(cls, image: bytes, result: ReceiptExtraction) -> None:
        cls.receipts[hashlib.sha256(image).hexdigest()] = result

    def __init__(self, fallback: Extractor | None = None):
        self.fallback = fallback

    def menu(self, image: bytes, media_type: str) -> MenuExtraction:
        key = hashlib.sha256(image).hexdigest()
        if key not in self.menus:
            if self.fallback is not None:
                return self.fallback.menu(image, media_type)
            raise ExtractionError("Photo reading isn't configured (no Anthropic API key). Enter items manually.")
        return self.menus[key]

    def receipt(self, images: list[tuple[bytes, str]]) -> ReceiptExtraction:
        key = hashlib.sha256(images[0][0]).hexdigest()
        if key not in self.receipts:
            if self.fallback is not None:
                return self.fallback.receipt(images)
            raise ExtractionError("Receipt reading isn't configured (no Anthropic API key). Enter lines manually.")
        return self.receipts[key]


_override: Extractor | None = None


def set_extractor(extractor: Extractor | None) -> None:
    global _override
    _override = extractor


def get_extractor(*, demo: bool = False) -> Extractor:
    if _override is not None:
        return _override
    real = ClaudeExtractor() if get_settings().extraction_enabled else None
    # Sample photos are recognised by hash; anything else goes to Claude when
    # a key is configured, or fails clearly (manual entry) when not.
    return CannedExtractor(fallback=real)


# ------------------------------------------------------------ verification
VEGAN_WORDS = ("vegan", "plant based", "plant-based")


def item_vegan_status(item: ExtractedItem, legend: dict[str, str]) -> tuple[dict, list[dict]]:
    """Decide the vegan badge, trusting only what is printed for 'marked'."""
    flags: list[dict] = []
    printed = " ".join([item.name, item.description]).lower()
    label_says_vegan = any(
        "vegan" in legend.get(lbl.strip().upper(), "").lower() or lbl.strip().upper() == "VG"
        for lbl in item.dietary_labels
    )
    word_says_vegan = any(w in printed for w in VEGAN_WORDS)
    if "vegan" in item.explicit_diet:
        if label_says_vegan or word_says_vegan:
            return {"status": "marked", "note": ""}, flags
        flags.append({"field": "dietary", "message": "Read as vegan, but no vegan label found — check."})
        return {"status": "possible", "note": "Unconfirmed reading of the menu"}, flags
    if item.vegan_on_request.strip():
        return {"status": "on_request", "note": item.vegan_on_request.strip()}, flags
    if item.possibly_vegan:
        return {"status": "possible", "note": item.possibly_vegan_reason.strip()}, flags
    return {"status": None, "note": ""}, flags


def check_price(text: str, cents: int | None, what: str) -> list[dict]:
    if not text.strip():
        return []
    parsed = parse_price(text)
    if parsed is not None and cents is not None and parsed != cents:
        return [{"field": "price", "message": f"{what}: printed '{text}' but read as {cents}c."}]
    return []


def receipt_flags(result: ReceiptExtraction) -> list[str]:
    flags: list[str] = []
    for ln in result.lines:
        if ln.kind in ("item", "surcharge", "discount", "tip") and ln.line_total_cents is None:
            flags.append(f"No amount could be read for '{ln.description}'.")
        if (
            ln.unit_price_cents is not None
            and ln.line_total_cents is not None
            and ln.quantity * ln.unit_price_cents != ln.line_total_cents
        ):
            flags.append(
                f"'{ln.description}': {ln.quantity} × {ln.unit_price_cents}c doesn't equal {ln.line_total_cents}c."
            )
    counted = sum(
        ln.line_total_cents or 0 for ln in result.lines if ln.kind in ("item", "surcharge", "discount", "tip")
    )
    if result.total_cents is None:
        flags.append("No total could be read from the receipt.")
    elif counted != result.total_cents:
        flags.append(f"The receipt's lines add up to {counted}c but its total says {result.total_cents}c.")
    return flags
