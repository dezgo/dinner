"""Uploaded photos.

Every upload is decoded with Pillow (so only real images are accepted),
rotated upright from its EXIF orientation, shrunk to a sensible size and
re-encoded as JPEG — which also drops all metadata, including GPS location.
Files are stored under a per-dinner folder with random names and are only
ever served through a route that checks the dinner.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings
from app.models import new_id

MAX_EDGE = 2400
_SAFE = re.compile(r"^[a-f0-9]{32}\.jpg$")


class ImageRejected(Exception):
    pass


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
            "That file isn't a photo this app can read. Use JPEG or PNG "
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
