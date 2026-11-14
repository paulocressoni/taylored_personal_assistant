"""assistant_node — the graph's single tool-using assistant.

The router / specialist / responder split is gone: the model itself decides
whether to answer directly or call a tool, so tool calling is the routing
mechanism. A response with neither text nor tool calls is unusable, so the node
recovers with one tools-free call instead of letting an empty answer reach the
user.
"""

import logging

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.core.llm import get_chat_model
from app.graph.nodes._shared import (
    build_system_text,
    complete_without_tools,
    role_for,
)
from app.graph.state import IPAState
from app.graph.utils import content_to_str, trim_messages
from app.prompts.assistant import RECOVERY_ADDENDUM
from app.tools.registry import TOOLS

logger = logging.getLogger(__name__)


async def assistant_node(state: IPAState, config: RunnableConfig) -> dict:
    """Answer the user, calling tools when the model decides they are needed.

    The unbound model is kept aside for the recovery path; only the normal call
    binds ``TOOLS``. The system prompt is assembled by the shared builder and is
    never persisted into ``state["messages"]``.

    Args:
        state: The current IPA state, including the persisted message history.
        config: The run configuration carrying callbacks and metadata.

    Returns:
        A state update containing the assistant's response message.
    """
    base = get_chat_model(role_for(state["channel"]))
    lang = state.get("lang")
    system = build_system_text(lang=lang, channel=state["channel"])
    history = trim_messages(state["messages"], settings.max_history_messages)
    messages = [SystemMessage(content=system), *history]

    response = await base.bind_tools(TOOLS).ainvoke(messages, config=config)

    # No tool call and no text is an unusable response (invalid tool call,
    # length-truncated before any output, or a provider quirk). Recover without
    # tools; a non-empty truncated answer is kept so streamed/spoken tokens
    # match the persisted reply.
    text = content_to_str(response.content).strip()
    if not response.tool_calls and not text:
        logger.warning(
            "assistant returned no usable content; recovering without tools "
            "(invalid_tool_calls=%d, finish_reason=%s)",
            len(response.invalid_tool_calls),
            response.response_metadata.get("finish_reason"),
        )
        recovery = await complete_without_tools(
            base,
            state["messages"],
            lang=lang,
            channel=state["channel"],
            addendum=RECOVERY_ADDENDUM,
            config=config,
        )
        return {"messages": [recovery]}

    return {"messages": [response]}
