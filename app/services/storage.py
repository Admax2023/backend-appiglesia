"""Private file storage for payment proofs.

Two backends, chosen by settings:
- Cloudinary (CLOUDINARY_URL set): images are uploaded as type "authenticated" under
  CLOUDINARY_FOLDER/YYYY/MM/<uuid4hex>, so they have no public URL. The DB keeps
  "cloudinary:<public_id>" and the API downloads them with a signed, short-lived URL.
- Local disk (default): files live under STORAGE_DIR/proofs/YYYY/MM/<uuid4hex>.<ext>. The DB keeps
  only the relative path. Names are generated server-side, so path traversal through user input is
  impossible; on read we additionally verify the resolved path stays inside STORAGE_DIR.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
import urllib.error
import urllib.request
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

import cloudinary
import cloudinary.uploader
import cloudinary.utils
from PIL import Image, UnidentifiedImageError

from app.config import get_settings
from app.errors import AppError

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_PIL_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_NAME_EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "jpe": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
MAX_PIXELS = 40_000_000  # decompression-bomb guard
CLOUDINARY_PREFIX = "cloudinary:"
_CLOUDINARY_TIMEOUT_SECONDS = 20

log = logging.getLogger(__name__)


@dataclass
class ValidatedImage:
    data: bytes
    mime_type: str
    ext: str
    sha256: str
    size: int
    original_filename: str | None


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sanitize_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\w.\- ]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:150] or None


def validate_image(data: bytes, filename: str | None) -> ValidatedImage:
    s = get_settings()
    if len(data) == 0:
        raise AppError(422, "empty_file", "El archivo está vacío")
    if len(data) > s.max_upload_bytes:
        raise AppError(413, "file_too_large", f"El archivo supera el máximo de {s.max_upload_bytes} bytes")
    mime = sniff_mime(data)
    if mime is None:
        raise AppError(415, "unsupported_file_type", "Solo se aceptan imágenes JPEG, PNG o WEBP")
    clean_name = sanitize_filename(filename)
    if clean_name and "." in clean_name:
        ext = clean_name.rsplit(".", 1)[1].lower()
        declared = _NAME_EXT_TO_MIME.get(ext)
        if declared is None or declared != mime:
            raise AppError(
                415, "file_type_mismatch", "La extensión del archivo no coincide con su contenido real"
            )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.format != _PIL_FORMAT[mime]:
                    raise AppError(415, "file_type_mismatch", "El contenido no corresponde a una imagen válida")
                w, h = img.size
                if w * h > MAX_PIXELS or w < 1 or h < 1:
                    raise AppError(422, "image_too_large", "Dimensiones de imagen no permitidas")
                img.verify()
            # verify() leaves the object unusable; do a full decode to catch truncated files
            with Image.open(io.BytesIO(data)) as img2:
                img2.load()
    except AppError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise AppError(415, "invalid_image", "El archivo no es una imagen válida")
    return ValidatedImage(
        data=data,
        mime_type=mime,
        ext=_EXT[mime],
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        original_filename=clean_name,
    )


def ext_for(mime_type: str) -> str:
    return _EXT.get(mime_type, "jpg")


def base_dir() -> Path:
    return get_settings().storage_dir.resolve()


@lru_cache
def _cloudinary_configured() -> bool:
    """Configures the SDK from CLOUDINARY_URL (cloudinary://<api_key>:<api_secret>@<cloud_name>)."""
    url = get_settings().cloudinary_url.strip()
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme != "cloudinary" or not (parts.hostname and parts.username and parts.password):
        raise RuntimeError("CLOUDINARY_URL debe tener el formato cloudinary://<api_key>:<api_secret>@<cloud_name>")
    cloudinary.config(
        cloud_name=parts.hostname, api_key=unquote(parts.username), api_secret=unquote(parts.password), secure=True
    )
    return True


def _storage_error() -> AppError:
    return AppError(502, "storage_unavailable", "No se pudo acceder al almacenamiento de comprobantes")


def save_proof(img: ValidatedImage) -> str:
    """Stores the image and returns its storage key (what PaymentProof.storage_path keeps)."""
    now = datetime.now(timezone.utc)
    name = f"{now:%Y}/{now:%m}/{uuid.uuid4().hex}"
    if _cloudinary_configured():
        folder = f"{get_settings().cloudinary_folder.strip('/')}/{now:%Y}/{now:%m}"
        public_id = f"{folder}/{name.rsplit('/', 1)[-1]}"
        try:
            res = cloudinary.uploader.upload(
                # asset_folder places it in the Media Library folder on accounts with dynamic folders
                img.data, public_id=public_id, asset_folder=folder, resource_type="image", type="authenticated",
                overwrite=False, unique_filename=False, use_filename=False,
                timeout=_CLOUDINARY_TIMEOUT_SECONDS,
            )
        except Exception:
            log.exception("Cloudinary upload failed")
            raise _storage_error()
        return CLOUDINARY_PREFIX + res["public_id"]
    rel = f"proofs/{name}.{img.ext}"
    abs_path = (base_dir() / rel).resolve()
    if not abs_path.is_relative_to(base_dir()):  # pragma: no cover - impossible by construction
        raise AppError(500, "storage_error", "Ruta de almacenamiento inválida")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(abs_path, "xb") as fh:
        fh.write(img.data)
    return rel


def resolve(rel: str) -> Path:
    base = base_dir()
    p = (base / rel).resolve()
    if not p.is_relative_to(base) or not p.is_file():
        raise AppError(404, "file_not_found", "Archivo no encontrado")
    return p


def read_proof(key: str, mime_type: str) -> bytes:
    """Returns the stored image bytes for a storage key."""
    if not key.startswith(CLOUDINARY_PREFIX):
        return resolve(key).read_bytes()
    if not _cloudinary_configured():
        raise _storage_error()
    public_id = key.removeprefix(CLOUDINARY_PREFIX)
    url = cloudinary.utils.private_download_url(
        public_id, ext_for(mime_type), resource_type="image", type="authenticated"
    )
    limit = get_settings().max_upload_bytes + 1
    try:
        with urllib.request.urlopen(url, timeout=_CLOUDINARY_TIMEOUT_SECONDS) as resp:
            return resp.read(limit)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AppError(404, "file_not_found", "Archivo no encontrado")
        log.error("Cloudinary download failed: HTTP %s", e.code)
        raise _storage_error()
    except OSError:
        log.exception("Cloudinary download failed")
        raise _storage_error()


def delete_quietly(key: str) -> None:
    """Best-effort removal (used to undo an upload whose DB row was not committed)."""
    try:
        if key.startswith(CLOUDINARY_PREFIX):
            if _cloudinary_configured():
                cloudinary.uploader.destroy(
                    key.removeprefix(CLOUDINARY_PREFIX), resource_type="image", type="authenticated",
                    invalidate=True, timeout=_CLOUDINARY_TIMEOUT_SECONDS,
                )
        else:
            (base_dir() / key).resolve().unlink(missing_ok=True)
    except Exception:
        log.warning("Could not delete stored proof %s", key, exc_info=True)
