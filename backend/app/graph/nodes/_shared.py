"""Shared helpers for the tools-free answer path.

The bound assistant call and the finalize/recovery calls must assemble the same
system text and apply the same history window; keeping both here prevents the
two paths from drifting. The chat model is passed in by the caller so each node
module keeps its own patchable ``get_chat_model`` seam.
"""

import logging
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.graph.utils import trim_messages
from app.language.detector import DEFAULT_LANG
from app.prompts.assistant import ASSISTANT_SYSTEM_PROMPT, VOICE_STYLE_ADDENDUM

logger = logging.getLogger(__name__)


def role_for(channel: str) -> Literal["assistant", "assistant_voice"]:
    """Return the LLM role for a channel.

    Args:
        channel: The transport that produced this turn ("voice", "text", "api").

    Returns:
        ``assistant_voice`` for voice turns, otherwise ``assistant``.
    """
    return "assistant_voice" if channel == "voice" else "assistant"


def build_system_text(
    *, lang: str | None, channel: str, addendum: str | None = None
) -> str:
    """Assemble the assistant system message in cache-friendly order.

    The static policy comes first, then the channel addendum, then the
    optional finalize/recovery notice, and only then the dynamic language line.
    Everything before the language line stays stable, so the provider's prefix
    cache is keyed by language rather than by turn.

    Args:
        lang: The resolved language tag, or None when nothing resolved it.
        channel: The transport that produced this turn.
        addendum: An optional finalize or recovery instruction.

    Returns:
        The complete system text.
    """
    parts = [ASSISTANT_SYSTEM_PROMPT]
    if channel == "voice":
        parts.append(VOICE_STYLE_ADDENDUM)
    if addendum:
        parts.append(addendum)

    # Concatenate rather than str.format: prompt examples may contain literal
    # braces, which format() would treat as fields.
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    return f"{text}\n\nReply in {lang or DEFAULT_LANG}."


async def complete_without_tools(
    model: BaseChatModel,
    messages: list[AnyMessage],
    *,
    lang: str | None,
    channel: str,
    addendum: str,
    config: RunnableConfig,
) -> AIMessage:
    """Run one tools-free completion over the capped history window.

    The helper owns the trim so the long tool-loop path cannot bypass
    ``max_history_messages``. Callers pass the full persisted history and an
    **unbound** model; binding tools here would defeat the tools-free contract.

    Args:
        model: The unbound chat model for the turn's channel.
        messages: The full persisted history for this session.
        lang: The resolved language tag, or None to fall back to the default.
        channel: The transport that produced this turn.
        addendum: The finalize or recovery instruction for this call.
        config: The run configuration carrying callbacks and metadata.

    Returns:
        The model's answer message.
    """
    history = trim_messages(messages, settings.max_history_messages)
    system = build_system_text(lang=lang, channel=channel, addendum=addendum)
    return await model.ainvoke([SystemMessage(content=system), *history], config=config)
