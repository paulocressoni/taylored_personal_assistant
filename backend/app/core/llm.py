"""LLM client factory (model-agnostic).

Callers pick a *role* (assistant / assistant_voice) and get back a fully
configured chat model. Model names and temperatures live only in ROLE_CONFIG,
so business logic never hardcodes a model name — swapping models or providers
is a one-place change.
"""

from functools import cache
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek

from app.core.config import settings


class RoleConfig(TypedDict):
    model: str
    temperature: float
    thinking: bool
    timeout: float
    max_tokens: int | None
    max_retries: int


ROLE_CONFIG: dict[str, RoleConfig] = {
    # thinking: False keeps the assistant cheap and fast — verified empirically
    # that flash thinks by default, so we must send "disabled" explicitly.
    "assistant": {
        "model": settings.deepseek_model_flash,
        "temperature": 0.6,
        "thinking": False,
        "timeout": 10.0,
        "max_tokens": 1024,
        "max_retries": 2,
    },
    # Voice answers are spoken, so the output is capped lower than text. 512
    # leaves room for a tool-call argument while keeping a runaway answer short.
    "assistant_voice": {
        "model": settings.deepseek_model_flash,
        "temperature": 0.6,
        "thinking": False,
        "timeout": 10.0,
        "max_tokens": 512,
        "max_retries": 2,
    },
}


@cache
def get_chat_model(role: str) -> BaseChatModel:
    """Return a configured chat model for the given role.

    A cached instance is created per call — models are stateless, so this is
    cheap and keeps callers free of shared mutable state.

    Raises:
        ValueError: if ``role`` is not a known key in ``ROLE_CONFIG``.
    """
    try:
        role_config = ROLE_CONFIG[role]
    except KeyError:
        valid = ", ".join(sorted(ROLE_CONFIG))
        raise ValueError(f"Unknown LLM role {role!r}. Valid roles: {valid}") from None

    # Explicit both ways: DeepSeek thinks by default, so "False" must send
    # "disabled" rather than omit the key — otherwise thinking stays on.
    extra_body = {
        "thinking": {"type": "enabled" if role_config["thinking"] else "disabled"}
    }

    return ChatDeepSeek(
        model=role_config["model"],
        temperature=role_config["temperature"],
        # api_key is already a SecretStr; ChatDeepSeek accepts it directly.
        api_key=settings.deepseek_api_key,
        max_tokens=role_config["max_tokens"],
        timeout=role_config["timeout"],
        max_retries=role_config["max_retries"],
        extra_body=extra_body,
    )
