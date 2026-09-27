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


def _is_placeholder(value: str) -> bool:
    """Treat the .env template's REPLACE_ME sentinels as "not configured"."""
    return "REPLACE_ME" in value


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

    # --- Runtime knobs ---
    # Dev conversation memory: the SQLite checkpointer DB file. Relative
    # paths resolve against the process CWD (backend/ locally, /app inside
    # the Docker image). A FILE (not ":memory:") survives both
    # `uvicorn --reload` restarts and container recreation.
    checkpoint_db_path: str = "checkpoints.db"

    # PROD conversation memory: when set, the graph switches to a Postgres
    # checkpointer (AsyncPostgresSaver) using this DSN and the SQLite file
    # above is ignored. The prod compose file injects this automatically;
    # leaving it unset keeps dev on plain SQLite with zero extra
    # infrastructure.
    checkpoint_db_url: str | None = None

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

    # --- Rate limiting ---
    # Per-API-key sliding-window limits for POST /chat and WS /ws/chat.
    # In-memory only (no Redis for dev): counts reset on restart and are not
    # shared across workers — fine for the single-process dev deployment.
    # Limits apply per presented API key. Set RATE_LIMIT_REQUESTS=0 to
    # disable.
    rate_limit_requests: int = 30
    rate_limit_window_seconds: float = 60.0

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

    # --- Voice (speech in/out) ---
    # Off by default so text-only deployments and CI need no audio accounts.
    # The validator below demands keys ONLY when this is switched on.
    voice_enabled: bool = False

    # STT is deliberately provider-agnostic: one OpenAI-compatible client covers
    # Groq, OpenAI and anything else that speaks /v1/audio/transcriptions, so
    # changing vendors is a config change rather than a code change. Groq's
    # whisper-large-v3-turbo is the cheap, fast, multilingual default.
    voice_stt_provider: Literal["groq", "openai"] = "groq"
    voice_stt_model: str = "whisper-large-v3-turbo"
    voice_stt_base_url: str = "https://api.groq.com/openai/v1"
    voice_stt_api_key: SecretStr = SecretStr("")
    # Hinting the language we already resolved improves both accuracy and
    # latency, but it stays a hint: detect_lang still owns the final answer.
    voice_stt_language_hint: bool = True
    voice_stt_timeout_seconds: float = 30.0

    # TTS. 24 kHz PCM is the lowest-latency format the vendor streams; hardware
    # sessions ask for 16 kHz instead (the reSpeaker XVF3800 I2S rate).
    voice_tts_provider: Literal["openai"] = "openai"
    voice_tts_model: str = "gpt-4o-mini-tts"
    voice_tts_base_url: str = "https://api.openai.com/v1"
    voice_tts_api_key: SecretStr = SecretStr("")
    # One voice per supported language tag; voice_for() falls back to "en".
    voice_tts_voices: dict[str, str] = {
        "en": "marin",
        "de": "cedar",
        "pt-BR": "coral",
    }
    # Spoken style, sent with every request — keep it short.
    voice_tts_instructions: str = "Speak naturally, warmly and clearly."
    voice_tts_timeout_seconds: float = 30.0
    # Synthesis is fired per sentence, not per reply, so the first audio byte
    # does not wait for the whole answer. These bound that splitter.
    voice_tts_min_chunk_chars: int = 40
    voice_tts_max_chunk_chars: int = 240

    # --- Voice audio contract ---
    # The browser captures/sends 16 kHz (Speech API native) and plays 24 kHz
    # (TTS native). A device declares its own rates in the session frame; the
    # server resamples only when they differ from the provider's output.
    voice_input_sample_rate: int = 16000
    voice_output_sample_rate: int = 24000
    # The reSpeaker XVF3800's I2S interface runs at 16 kHz (stereo, 32-bit),
    # so a hardware session declares 16 kHz for both directions and needs no
    # resampling anywhere; only the browser plays back at the TTS-native 24 kHz.

    # Server-side endpointing (Silero VAD).
    # Thresholds mirror the upstream wrapper's defaults (0.5 / threshold-0.15):
    # speech starts ABOVE threshold and only ends once it drops BELOW
    # neg_threshold, so a probability hovering near 0.5 cannot chop one
    # sentence into several.
    voice_vad_threshold: float = 0.5
    voice_vad_neg_threshold: float = 0.35
    # Short blips below this are discarded as noise rather than treated as a
    # turn; padding re-adds a few ms around a segment so the first and last
    # phoneme survive for STT.
    voice_vad_min_speech_ms: int = 150
    voice_vad_speech_pad_ms: int = 30
    # The main latency/robustness dial, and deliberately 5x the upstream
    # default of 100 ms: we would rather wait out a thinking pause than cut
    # the user off mid-thought, and pay for it in ~400 ms of extra latency.
    voice_vad_min_silence_ms: int = 500
    voice_vad_max_utterance_s: float = 30.0

    # One long-lived socket per device or browser tab. A client that sends
    # neither audio nor keepalives for this long is dropped, so a vanished
    # client cannot hold a slot (and a cloud quota) forever. Hardware pings
    # roughly every 20 s, so the timeout never fires for a healthy device.
    voice_idle_timeout_seconds: float = 60.0
    voice_max_sessions: int = 3

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

    @property
    def has_voice_stt(self) -> bool:
        """True when an STT key is present, whatever the provider."""
        return bool(self.voice_stt_api_key.get_secret_value()) and not _is_placeholder(
            self.voice_stt_api_key.get_secret_value()
        )

    @property
    def has_voice_tts(self) -> bool:
        """True when a TTS key is present, whatever the provider."""
        return bool(self.voice_tts_api_key.get_secret_value()) and not _is_placeholder(
            self.voice_tts_api_key.get_secret_value()
        )

    @property
    def voice_ready(self) -> bool:
        """True only when voice is switched on AND both providers are keyed."""
        return self.voice_enabled and self.has_voice_stt and self.has_voice_tts

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

        # Voice is opt-in, so its keys are demanded only when it is switched on.
        # That keeps text-only installs (and CI, which copies .env.dev.example)
        # free of vendor accounts, while still failing loudly at startup rather
        # than mid-conversation.
        if self.voice_enabled:
            if not self.has_voice_stt:
                raise ValueError(
                    "VOICE_ENABLED=true requires VOICE_STT_API_KEY (Groq or "
                    f"OpenAI). Add it to .env.{_APP_ENV} or set the "
                    "VOICE_STT_API_KEY env var."
                )

            if not self.has_voice_tts:
                raise ValueError(
                    "VOICE_ENABLED=true requires VOICE_TTS_API_KEY (OpenAI). "
                    f"Add it to .env.{_APP_ENV} or set the VOICE_TTS_API_KEY "
                    "env var."
                )

            # Hysteresis needs the stop threshold at or below the start
            # threshold: speech begins above the higher value and only ends
            # below the lower one.
            if self.voice_vad_neg_threshold > self.voice_vad_threshold:
                raise ValueError(
                    "VOICE_VAD_NEG_THRESHOLD must be at or below "
                    "VOICE_VAD_THRESHOLD, otherwise endpointing is undefined."
                )

            # Silero's analysis window is 512 samples, which is 32 ms only at
            # 16 kHz, and app/voice/vad.py is built around that. Any other rate
            # would mis-window every utterance without raising anything.
            if self.voice_input_sample_rate != 16000:
                raise ValueError(
                    "VOICE_INPUT_SAMPLE_RATE must be 16000: the Silero VAD "
                    "window and the capture contract both assume it."
                )

            # A language we can recognise but cannot speak is a config bug, and
            # it is far cheaper to catch here than mid-conversation.
            missing_voices = [
                lang
                for lang in self.supported_languages
                if lang not in self.voice_tts_voices
            ]
            if missing_voices:
                raise ValueError(
                    "VOICE_TTS_VOICES has no voice for supported language(s): "
                    f"{', '.join(missing_voices)}. Add one entry per language."
                )

        return self


# Step 2 — module-level instance: importing app.core.config resolves settings
# exactly once. This is what makes validation fail fast at startup.
settings = Settings()
