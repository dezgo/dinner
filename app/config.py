"""Bootstrap configuration, read once from the environment.

Secrets (the organiser password, the Up token, the Anthropic key) live only
here and in the server's `.env`. None of them is ever rendered into a page or
sent to a browser.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "production", "test"]
DEV_SECRET = "dev-secret-change-me"  # noqa: S105 — refused in production below


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Environment = "development"
    log_level: str = "INFO"

    # Signs session cookies and derives the key that encrypts the Up webhook
    # secret at rest. Changing it signs everyone out and forces the webhook to
    # be re-registered.
    secret_key: str = DEV_SECRET

    # The organiser's password. Blank disables organiser sign-in entirely.
    admin_password: str = ""

    database_url: str = "sqlite:///./data/dinnertab.db"
    # Uploaded menu and receipt photos. Never served statically: every image
    # request is checked against the dinner it belongs to.
    upload_dir: Path = Path("./data/uploads")
    max_upload_mb: int = 15

    trusted_hosts: str = "*"
    trust_proxy_headers: bool = False
    # What the QR code points at. Blank derives it from the request.
    public_base_url: str = ""

    # ------------------------------------------------------------ extraction
    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-5-5"
    extraction_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # ---------------------------------------------------------------- up bank
    up_api_token: str = ""
    up_api_base: str = "https://api.up.com.au/api/v1"
    # How often to re-read recent transactions while any payment window is
    # open, as a safety net for missed webhooks.
    up_poll_minutes: int = 5
    # Payment windows close this many days after a bill is finalised.
    payment_window_days: int = 30

    # Shows the "simulate a payment" tool on demo dinners. Simulated payments
    # never touch a bank and can only ever match demo dinners.
    demo_tools: bool = True

    @model_validator(mode="after")
    def _production_needs_real_secrets(self) -> Settings:
        if self.env == "production":
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be set to a long random value in production.")
            if len(self.admin_password) < 12:
                raise ValueError("ADMIN_PASSWORD must be at least 12 characters in production.")
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def extraction_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def up_enabled(self) -> bool:
        return bool(self.up_api_token)

    @property
    def trusted_host_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
