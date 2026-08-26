"""Application configuration (12-factor, fail-fast).

Loads from ``.env.{ENV}`` where ``ENV`` must be set to ``dev`` or ``prod``.
Missing/invalid required settings crash at startup, never mid-request.
"""

import os
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_ENVS = ("dev", "prod")

# Step 1 — chicken-and-egg: ENV must exist in the OS environment BEFORE
# pydantic runs, because it decides which .env file to load.
_APP_ENV = os.environ.get("ENV")
if _APP_ENV is None:
    raise RuntimeError(
        "The ENV environment variable is required. Set ENV=dev or ENV=prod."
    )
if _APP_ENV not in VALID_ENVS:
    raise RuntimeError(f"Invalid ENV={_APP_ENV!r}. Must be one of {VALID_ENVS}.")


class Settings(BaseSettings):
    """Settings for the application."""

    # Configuration for pydantic-settings
    model_config = SettingsConfigDict(
        env_file=f".env.{_APP_ENV}",  # <-- dynamic file selection
        env_file_encoding="utf-8",
        extra="ignore",  # ignore stray vars, don't blow up on them
    )

    # Defaults exist only so static checkers accept `Settings()`.
    # At runtime pydantic overrides them from env vars / .env, and the
    # validator below keeps the "fail fast at startup" guarantee.
    env: Literal["dev", "prod"] = "dev"
    deepseek_api_key: SecretStr = SecretStr("")

    # Deterministic settings with sane defaults (NOT vector memory)
    default_timezone: str = "Europe/Berlin"
    supported_languages: list[str] = ["en", "de", "pt-BR"]

    # --- Observability (Langfuse) ---
    # Deliberately OPTIONAL and best-effort: a down or unconfigured
    # observability stack must NEVER block the assistant. The validator
    # below does NOT require these — only DeepSeek stays fail-fast.
    langfuse_enabled: bool = False
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    # v4 SDK env var name is LANGFUSE_BASE_URL (LANGFUSE_HOST is deprecated).
    langfuse_base_url: str = "http://localhost:3000"

    # Check if DeepSeek API key is available
    @property
    def has_deepseek(self) -> bool:
        # SecretStr holds the real value in .get_secret_value(); str() masks it.
        return bool(self.deepseek_api_key.get_secret_value())

    @property
    def langfuse_ready(self) -> bool:
        """True only when observability is switched on AND fully keyed."""
        return self.langfuse_enabled and bool(
            self.langfuse_public_key.get_secret_value()
            and self.langfuse_secret_key.get_secret_value()
        )

    @model_validator(mode="after")
    def _fail_fast_on_missing_required(self) -> "Settings":
        if not self.deepseek_api_key.get_secret_value():
            raise ValueError(
                "DEEPSEEK_API_KEY is required but was not found. "
                f"Add it to .env.{_APP_ENV} or set the DEEPSEEK_API_KEY env var."
            )
        return self


# Step 2 — module-level instance: importing app.core.config resolves settings
# exactly once. This is what makes validation fail fast at startup.
settings = Settings()
