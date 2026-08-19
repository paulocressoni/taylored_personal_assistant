"""LLM client factory (model-agnostic).

Callers pick a *role* (router / specialist / responder) and get back a fully
configured chat model. Model names and temperatures live only in ROLE_CONFIG,
so business logic never hardcodes a model name — swapping models or providers
is a one-place change.
"""

from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek

from app.core.config import settings


class RoleConfig(TypedDict):
    model: str
    temperature: float
    thinking: bool


ROLE_CONFIG: dict[str, RoleConfig] = {
    # thinking: False keeps cheap roles cheap — verified empirically that
    # v4-flash thinks by default, so we must send "disabled" explicitly.
    "router": {"model": "deepseek-v4-flash", "temperature": 0.0, "thinking": False},
    "specialist": {"model": "deepseek-v4-flash", "temperature": 0.0, "thinking": False},
    "responder": {"model": "deepseek-v4-flash", "temperature": 1.3, "thinking": False},
}


def get_chat_model(role: str) -> BaseChatModel:
    """Return a configured chat model for the given role.

    A fresh instance is created per call — models are stateless, so this is
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
        max_tokens=None,
        timeout=None,
        max_retries=2,
        extra_body=extra_body,
    )
