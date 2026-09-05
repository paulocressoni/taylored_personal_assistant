"""Application configuration (12-factor, fail-fast).

Loads from ``.env.{ENV}`` where ``ENV`` must be set to ``dev`` or ``prod``.
Missing/invalid required settings crash at startup, never mid-request.
"""

import os
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SUPPORTED_LANGS is the CANONICAL language list and lives in a leaf module
# (it imports nothing from the app). We import it here so
# Settings.supported_languages defaults from a single source of truth instead
# of duplicating the literal (CFG-02).
from app.language.detector import SUPPORTED_LANGS

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
    """Settings for the application.

    Every field maps to an env var of the same name, case-insensitively
    (e.g. ``checkpoint_db_path`` <-> ``CHECKPOINT_DB_PATH``). Values load from
    the ``.env.{ENV}`` file chosen above, then from the process environment.
    """

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

    # Shared API key clients must present (X-API-Key header on HTTP,
    # ?api_key= query param on WebSocket). Required, fail-fast like the
    # DeepSeek key, so auth can never silently be off.
    assistant_api_key: SecretStr = SecretStr("")

    # Deterministic settings with sane defaults (NOT vector memory)
    default_timezone: str = "Europe/Berlin"
    # Single source of truth for supported language tags lives in
    # app.language.detector.SUPPORTED_LANGS (a leaf module). We DEFAULT from it
    # here; the SUPPORTED_LANGUAGES env var can still override at runtime
    # (CFG-02 — test_langdetect.py guards the two from drifting).
    supported_languages: list[str] = SUPPORTED_LANGS

    # --- Runtime knobs (BE-04 / BE-05) ---
    # Where the SQLite checkpointer DB file is created. Relative paths resolve
    # against the process CWD: backend/ when run locally, /app inside the
    # Docker image (the compose named volume mounts over /app). A FILE (not
    # ":memory:") survives both `uvicorn --reload` restarts and container
    # recreation.
    checkpoint_db_path: str = "checkpoints.db"

    # Hard deadline (seconds) for ONE graph run, applied in POST /chat and the
    # /ws/chat stream so a hung upstream model can never hold a request open.
    graph_timeout_seconds: float = 90.0

    # Cap on how many conversation messages a node forwards to the
    # model. The checkpointer keeps the FULL history (the session's memory,
    # read back whole by GET /sessions/{id}/history) — this only bounds the
    # window the responder / knowledge specialist build their prompt from,
    # so a long session can't exceed the model's context window or bloat
    # every LLM call. Override with MAX_HISTORY_MESSAGES. 0 / negative =
    # no cap.
    max_history_messages: int = 20

    # DeepSeek model names (v4-flash is cheaper, v4-pro is more capable).
    # The default is hardcoded here so we have a single source of truth for the default models.
    deepseek_model_flash: str = "deepseek-v4-flash"
    deepseek_model_pro: str = "deepseek-v4-pro"

    # Browser origins allowed to call this API directly (e.g. CORS_ORIGINS=
    # '["http://localhost:5173"]'). Empty by default = same-origin only: the
    # stock dev UI talks to :5173 and the Vite proxy forwards, so it needs no
    # CORS headers. Never use a wildcard with credentials.
    cors_origins: list[str] = []

    # --- Observability (Langfuse) ---
    # Deliberately OPTIONAL and best-effort: a down or unconfigured
    # observability stack must NEVER block the assistant. The validator
    # below does NOT require these — only DeepSeek and the assistant API key stay fail-fast.
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
    def has_assistant(self) -> bool:
        """Check if the shared API key is available."""
        return bool(self.assistant_api_key.get_secret_value())

    @property
    def langfuse_ready(self) -> bool:
        """True only when observability is switched on AND fully keyed."""
        return self.langfuse_enabled and bool(
            self.langfuse_public_key.get_secret_value()
            and self.langfuse_secret_key.get_secret_value()
        )

    @model_validator(mode="after")
    def _fail_fast_on_missing_required(self) -> "Settings":
        """Fail fast on missing required settings."""
        if not self.deepseek_api_key.get_secret_value():
            raise ValueError(
                "DEEPSEEK_API_KEY is required but was not found. "
                f"Add it to .env.{_APP_ENV} or set the DEEPSEEK_API_KEY env var."
            )
        if not self.assistant_api_key.get_secret_value():
            raise ValueError(
                "ASSISTANT_API_KEY is required but was not found. "
                f"Add it to .env.{_APP_ENV} or set the ASSISTANT_API_KEY env var."
            )
        return self


# Step 2 — module-level instance: importing app.core.config resolves settings
# exactly once. This is what makes validation fail fast at startup.
settings = Settings()
