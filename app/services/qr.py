from __future__ import annotations

import io

import qrcode
import qrcode.image.svg


def qr_svg(url: str) -> bytes:
    img = qrcode.make(
        url, image_factory=qrcode.image.svg.SvgPathImage, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M
    )
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
