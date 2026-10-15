"""Printable QR code for the public menu URL (see docs/QR.md).

    python -m app.cli qr                                   # PUBLIC_WEB_URL -> menu-qr.png
    python -m app.cli qr --url https://pedidos.example.org --output menu-qr.svg
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import qrcode
import qrcode.constants
from qrcode.image.svg import SvgPathImage

ERROR_LEVELS = {
    "L": qrcode.constants.ERROR_CORRECT_L,  # ~7 % recoverable
    "M": qrcode.constants.ERROR_CORRECT_M,  # ~15 % (default: good for clean prints)
    "Q": qrcode.constants.ERROR_CORRECT_Q,  # ~25 % (laminated / outdoor posters)
    "H": qrcode.constants.ERROR_CORRECT_H,  # ~30 %
}


def validate_url(url: str) -> str:
    url = url.strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"URL inválida para el QR (debe empezar por http:// o https://): {url!r}")
    return url


def build(url: str, error: str = "M", border: int = 4, box_size: int = 20) -> qrcode.QRCode:
    qr = qrcode.QRCode(
        version=None, error_correction=ERROR_LEVELS[error.upper()], box_size=box_size, border=border
    )
    qr.add_data(validate_url(url))
    qr.make(fit=True)
    return qr


def write(url: str, output: Path, error: str = "M", border: int = 4, box_size: int = 20) -> Path:
    """Write the QR as PNG (raster, ~box_size px per module) or SVG (vector, best for printing)."""
    qr = build(url, error, border, box_size)
    suffix = output.suffix.lower()
    if suffix == ".svg":
        img = qr.make_image(image_factory=SvgPathImage)
    elif suffix == ".png":
        img = qr.make_image(fill_color="black", back_color="white")
    else:
        raise ValueError("La salida debe terminar en .png o .svg")
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as fh:
        img.save(fh)
    return output
