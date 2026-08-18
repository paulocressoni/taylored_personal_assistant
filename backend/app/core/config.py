"""Application configuration (12-factor, fail-fast).

Loads from ``.env.{ENV}`` where ``ENV`` must be set to ``dev`` or ``prod``.
Missing/invalid required settings crash at startup, never mid-request.
"""

import os
from typing import Literal

from pydantic import SecretStr
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

    env: Literal["dev", "prod"]
    deepseek_api_key: SecretStr

    # Deterministic settings with sane defaults (NOT vector memory)
    default_timezone: str = "Europe/Berlin"
    supported_languages: list[str] = ["en", "de", "pt-BR"]

    # Check if DeepSeek API key is available
    @property
    def has_deepseek(self) -> bool:
        # SecretStr holds the real value in .get_secret_value(); str() masks it.
        return bool(self.deepseek_api_key.get_secret_value())


# Step 2 — module-level instance: importing app.core.config resolves settings
# exactly once. This is what makes validation fail fast at startup.
settings = Settings()
