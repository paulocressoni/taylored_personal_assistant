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


def test_voice_enabled_requires_a_voice_per_language() -> None:
    with pytest.raises(ValueError, match="no voice for supported language"):
        Settings(
            _env_file=None,
            deepseek_api_key="sk-test-key",  # pragma: allowlist secret
            assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
            voice_enabled=True,
            voice_stt_api_key="gsk-test",  # pragma: allowlist secret
            voice_tts_api_key="sk-tts-test",  # pragma: allowlist secret
            voice_tts_voices={"en": "marin"},
        )


def test_placeholder_keys_do_not_count_as_configured() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-test-key",  # pragma: allowlist secret
        assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
        voice_stt_api_key="gsk_REPLACE_ME",  # pragma: allowlist secret
        voice_tts_api_key="sk-REPLACE_ME",  # pragma: allowlist secret
    )
    assert settings.has_voice_stt is False
    assert settings.has_voice_tts is False


def test_voice_vad_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="VOICE_VAD_NEG_THRESHOLD"):
        Settings(
            _env_file=None,
            deepseek_api_key="sk-test-key",  # pragma: allowlist secret
            assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
            voice_enabled=True,
            voice_stt_api_key="gsk-test",  # pragma: allowlist secret
            voice_tts_api_key="sk-tts-test",  # pragma: allowlist secret
            voice_vad_threshold=0.3,
            voice_vad_neg_threshold=0.6,
        )


def test_voice_input_sample_rate_is_pinned_to_the_vad_window() -> None:
    with pytest.raises(ValueError, match="VOICE_INPUT_SAMPLE_RATE"):
        Settings(
            _env_file=None,
            deepseek_api_key="sk-test-key",  # pragma: allowlist secret
            assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
            voice_enabled=True,
            voice_stt_api_key="gsk-test",  # pragma: allowlist secret
            voice_tts_api_key="sk-tts-test",  # pragma: allowlist secret
            voice_input_sample_rate=48000,
        )


# --- wake word settings -----------------------------------------------------


def _voice_settings(**overrides):
    """Settings that pass the voice validators, so a wake rule can be isolated."""
    base = {
        "_env_file": None,
        "deepseek_api_key": "sk-test-key",  # pragma: allowlist secret
        "assistant_api_key": "sk-assistant-test",  # pragma: allowlist secret
        "voice_enabled": True,
        "voice_stt_api_key": "gsk-test",  # pragma: allowlist secret
        "voice_tts_api_key": "sk-tts-test",  # pragma: allowlist secret
    }
    base.update(overrides)
    return Settings(**base)


def test_wake_word_defaults_keep_the_feature_off() -> None:
    settings = Settings()

    assert settings.voice_wake_word_enabled is False
    assert settings.voice_wake_word_model == "hey_jarvis_v0.1.onnx"
    assert settings.voice_wake_word_phrase == ""
    assert settings.voice_wake_word_threshold == 0.5
    assert settings.voice_wake_word_window_seconds == 6.0
    assert settings.voice_wake_word_allow_opt_out is True


@pytest.mark.parametrize(
    "model",
    ["openwakeword/hey_jarvis_v0.1.onnx", "openwakeword\\hey_jarvis_v0.1.onnx"],
)
def test_a_wake_model_with_a_path_separator_is_rejected(model: str) -> None:
    # `Path(name).name == name` alone would accept the backslash on Linux, where
    # it is a legal filename character — the rule has to mean the same thing on
    # every host that reads the setting.
    with pytest.raises(ValueError, match="bare .onnx filename"):
        _voice_settings(voice_wake_word_model=model)


def test_a_wake_model_that_is_not_onnx_is_rejected() -> None:
    with pytest.raises(ValueError, match="bare .onnx filename"):
        _voice_settings(voice_wake_word_model="hey_jarvis_v0.1.tflite")


def test_forbidding_opt_out_while_the_gate_is_off_is_a_startup_error() -> None:
    # Every session would resolve to un-gated and then be refused: voice could
    # not start at all. Fail at import instead of bricking the feature.
    with pytest.raises(ValueError, match="ALLOW_OPT_OUT=false requires"):
        Settings(
            _env_file=None,
            deepseek_api_key="sk-test-key",  # pragma: allowlist secret
            assistant_api_key="sk-assistant-test",  # pragma: allowlist secret
            voice_wake_word_enabled=False,
            voice_wake_word_allow_opt_out=False,
        )
