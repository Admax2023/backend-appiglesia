from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import PaymentProof
from app.services import storage
from tests.conftest import image_bytes


class _FakeCloudinary:
    """In-memory stand-in for the Cloudinary upload/download/destroy calls."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploads: list[dict] = []

    def upload(self, data, **kw):
        self.uploads.append(kw)
        self.files[kw["public_id"]] = data
        return {"public_id": kw["public_id"]}

    def destroy(self, public_id, **kw):
        self.files.pop(public_id, None)

    def private_download_url(self, public_id, fmt, **kw):
        return f"fake://{public_id}"

    def urlopen(self, url, timeout):
        return io.BytesIO(self.files[url.removeprefix("fake://")])


@pytest.fixture
def fake_cloudinary(monkeypatch):
    fake = _FakeCloudinary()
    monkeypatch.setattr(get_settings(), "cloudinary_url", "cloudinary://key:secret@demo")
    storage._cloudinary_configured.cache_clear()
    monkeypatch.setattr(storage.cloudinary.uploader, "upload", fake.upload)
    monkeypatch.setattr(storage.cloudinary.uploader, "destroy", fake.destroy)
    monkeypatch.setattr(storage.cloudinary.utils, "private_download_url", fake.private_download_url)
    monkeypatch.setattr(storage.urllib.request, "urlopen", fake.urlopen)
    yield fake
    storage._cloudinary_configured.cache_clear()


def test_proof_stored_privately_in_cloudinary(fake_cloudinary, client, make_product, place_order, auth, db):
    p = make_product(price="8000")
    o = place_order([(p, 1)], method="transfer").json()
    r = client.post(
        f"/api/public/orders/{o['tracking_token']}/proofs",
        data={"declared_amount": "8000"},
        files={"file": ("foto.png", image_bytes(), "image/png")},
    )
    assert r.status_code == 201, r.text

    up = fake_cloudinary.uploads[0]
    assert up["type"] == "authenticated" and up["public_id"].startswith("iglesia/comprobantes/")
    row = db.scalar(select(PaymentProof))
    assert row.storage_path == storage.CLOUDINARY_PREFIX + up["public_id"]

    proof = client.get("/api/admin/proofs", headers=auth).json()["items"][0]
    f = client.get(proof["file_url"], headers=auth)
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
    assert f.content == image_bytes()


def test_cloudinary_delete_quietly(fake_cloudinary):
    fake_cloudinary.files["iglesia/comprobantes/x"] = b"img"
    storage.delete_quietly(storage.CLOUDINARY_PREFIX + "iglesia/comprobantes/x")
    assert fake_cloudinary.files == {}
