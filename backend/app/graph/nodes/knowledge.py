"""knowledge_node — a specialist bound to the tool registry.

Unlike router/responder (which build input from state fields via a
ChatPromptTemplate), this node passes the RAW message history to the model so
tool calls and results flow through the ReAct loop. The system prompt is
prepended locally per call — never written into state["messages"], so it
doesn't leak into the responder or persisted history.
"""

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.core.llm import get_chat_model
from app.graph.state import IPAState
from app.graph.utils import trim_messages
from app.prompts.knowledge import KNOWLEDGE_SYSTEM_PROMPT
from app.tools.registry import TOOLS


async def knowledge_node(state: IPAState, config: RunnableConfig) -> dict:
    """Handle knowledge requests with a tool-bound specialist model.

    Passes the CAPPED history window to the specialist so the ReAct loop never
    grows unbounded; the system prompt is prepended locally and the persisted
    ``state["messages"]`` is never mutated. The model is awaited asynchronously
    so request-level cancellation reaches the provider request.

    Args:
        state: The current IPA state, including the message history.
        config: The run configuration carrying callbacks and metadata.

    Returns:
        A state update containing the specialist's response message.
    """
    model = get_chat_model("specialist").bind_tools(TOOLS)
    # Cap the ReAct history handed to the specialist. The system prompt is
    # prepended per call and never written into state["messages"].
    history = trim_messages(state["messages"], settings.max_history_messages)
    messages = [SystemMessage(content=KNOWLEDGE_SYSTEM_PROMPT), *history]

    response = await model.ainvoke(messages, config=config)

    return {"messages": [response]}
