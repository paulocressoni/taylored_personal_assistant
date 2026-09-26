"""Fast unit tests for ``app.core.config`` — no network, no real secrets.

Run:  cd backend && uv run pytest
Requires ``ENV`` to be set (e.g. ``ENV=dev make test``) so the module can
resolve its environment file — that is the fail-fast design, not a bug.
"""

import pytest

from app.core.config import VALID_ENVS, Settings


def test_valid_envs_are_dev_and_prod() -> None:
    assert VALID_ENVS == ("dev", "prod")


def test_settings_resolve_defaults_from_env_file() -> None:
    """With ENV=dev set, settings resolve deterministic values from .env.dev."""
    settings = Settings()
    assert settings.env == "dev"
    assert settings.default_timezone == "Europe/Berlin"
    assert settings.supported_languages == ["en", "de", "pt-BR"]


def test_has_deepseek_true_when_key_provided() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-test-key",  # pragma: allowlist secret
        assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
    )
    assert settings.has_deepseek is True


def test_has_api_key_true_when_key_provided() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-test-key",  # pragma: allowlist secret
        assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
    )
    assert settings.has_assistant is True


def test_fail_fast_on_missing_api_key() -> None:
    """An empty DEEPSEEK_API_KEY must raise loudly at construction time."""
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY is required"):
        Settings(_env_file=None, deepseek_api_key="")


def test_voice_disabled_by_default() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-test-key",  # pragma: allowlist secret
        assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
    )
    assert settings.voice_enabled is False
    assert settings.voice_ready is False


def test_voice_enabled_requires_stt_key() -> None:
    """Switching voice on without keys must fail loudly at construction time."""
    with pytest.raises(ValueError, match="VOICE_STT_API_KEY"):
        Settings(
            _env_file=None,
            deepseek_api_key="sk-test-key",  # pragma: allowlist secret
            assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
            voice_enabled=True,
        )
