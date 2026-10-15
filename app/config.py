from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Fragments of well-known example values; a secret containing any of them is refused at startup.
_PLACEHOLDER_SECRETS = ("change-me", "changeme", "change_me", "your-secret", "secret-key", "placeholder")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = "development"
    database_url: str = "postgresql+psycopg://iglesia:iglesia_dev_password@localhost:5433/iglesia"
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    cors_origins: str = "http://localhost:5173"
    storage_dir: Path = BACKEND_DIR / "storage_private"
    # When set (cloudinary://<api_key>:<api_secret>@<cloud_name>), payment proofs go to Cloudinary
    # as private "authenticated" images instead of STORAGE_DIR.
    cloudinary_url: str = ""
    cloudinary_folder: str = "iglesia/comprobantes"
    max_upload_bytes: int = 5 * 1024 * 1024
    max_pending_proofs_per_order: int = 3
    max_json_body_bytes: int = 64 * 1024
    default_timezone: str = "America/Bogota"

    rate_limit_enabled: bool = True
    # Behind a reverse proxy: the client IP is taken from X-Forwarded-For counting TRUSTED_PROXY_HOPS
    # entries from the RIGHT (the leftmost entries are client-controlled and never trusted).
    trust_proxy_headers: bool = False
    trusted_proxy_hops: int = Field(default=1, ge=1, le=5)
    # Stored idempotency responses are deleted after this many hours (background task + CLI).
    idempotency_retention_hours: int = Field(default=48, ge=1)
    # How often the in-process purge runs (0 disables it, e.g. when a cron job runs the CLI instead).
    idempotency_purge_interval_minutes: int = Field(default=60, ge=0)
    duplicate_order_window_seconds: int = 120
    public_web_url: str = "http://localhost:5173"

    # Business limits on orders
    max_order_lines: int = 30
    max_quantity_per_line: int = 50

    # Allowed COP denominations for the cash count (bills and coins).
    bill_denominations: list[Decimal] = [
        Decimal(x) for x in ("100000", "50000", "20000", "10000", "5000", "2000", "1000")
    ]
    coin_denominations: list[Decimal] = [
        Decimal(x) for x in ("1000", "500", "200", "100", "50")
    ]

    @field_validator("jwt_secret", mode="after")
    @classmethod
    def _reject_placeholder_secret(cls, v: str) -> str:
        low = v.lower()
        if any(p in low for p in _PLACEHOLDER_SECRETS):
            raise ValueError("JWT_SECRET contiene un valor de ejemplo: genera uno con secrets.token_urlsafe(48)")
        return v

    @model_validator(mode="after")
    def _production_secret_strength(self) -> "Settings":
        # In production also refuse low-entropy secrets (e.g. "aaaa...", "1234...").
        if self.is_production and len(set(self.jwt_secret)) < 16:
            raise ValueError("JWT_SECRET tiene muy poca entropía para producción: usa secrets.token_urlsafe(48)")
        return self

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _abs_storage(cls, v: Path) -> Path:
        if not v.is_absolute():
            v = (BACKEND_DIR / v).resolve()
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
