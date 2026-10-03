"""Uploaded photos.

Every upload is decoded with Pillow (so only real images are accepted),
rotated upright from its EXIF orientation, shrunk to a sensible size and
re-encoded as JPEG — which also drops all metadata, including GPS location.
Files are stored under a per-dinner folder with random names and are only
ever served through a route that checks the dinner.

A PDF (say a menu downloaded from the restaurant's website) is turned into one
image per page first, so from then on it's handled exactly like photos.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings
from app.models import new_id

MAX_EDGE = 2400
_SAFE = re.compile(r"^[a-f0-9]{32}\.jpg$")


class ImageRejected(Exception):
    pass


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF")


def split_uploads(blobs: list[bytes], max_pages: int) -> list[bytes]:
    """Each upload as page images: a photo stays one, a PDF becomes one per page."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    pages: list[bytes] = []
    for data in blobs:
        if len(data) > limit:
            raise ImageRejected(f"That file is over {get_settings().max_upload_mb} MB.")
        pages.extend(pdf_pages(data) if is_pdf(data) else [data])
    if len(pages) > max_pages:
        raise ImageRejected(
            f"That's {len(pages)} pages; the most at once is {max_pages}. Send the first few, then the rest."
        )
    return pages


def pdf_pages(data: bytes) -> list[bytes]:
    """Render every page of a PDF to PNG, about MAX_EDGE pixels on its long side."""
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ImageRejected("That PDF couldn't be opened (it may be damaged or password protected).") from e
    out: list[bytes] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            width, height = page.get_size()
            scale = min(MAX_EDGE / max(width, height, 1), 4)
            img = page.render(scale=scale).to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out.append(buf.getvalue())
    finally:
        pdf.close()
    if not out:
        raise ImageRejected("That PDF has no pages.")
    return out


def dinner_dir(dinner_id: str) -> Path:
    path = get_settings().upload_dir / dinner_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_image(dinner_id: str, data: bytes) -> str:
    limit = get_settings().max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ImageRejected(f"That photo is over {get_settings().max_upload_mb} MB.")
    name = f"{new_id()}.jpg"
    (dinner_dir(dinner_id) / name).write_bytes(normalise(data))
    return name


def normalise(data: bytes) -> bytes:
    """Decode, orient, shrink and re-encode. Deterministic for the same input."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise ImageRejected(
            "That file isn't a photo or PDF this app can read. Use JPEG, PNG or PDF "
            "(on iPhone, choose 'Most Compatible' in Camera settings)."
        ) from e
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def image_path(dinner_id: str, name: str) -> Path | None:
    if not _SAFE.match(name):
        return None
    path = get_settings().upload_dir / dinner_id / name
    return path if path.is_file() else None


def read_image(dinner_id: str, name: str) -> bytes:
    path = image_path(dinner_id, name)
    if path is None:
        raise FileNotFoundError(name)
    return path.read_bytes()
