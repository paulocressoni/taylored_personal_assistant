"""finalize_node — tools-free wrap-up after the tool iteration cap.

``tool_exec`` routes here when the cap is reached. The prompt carries the
"limit reached" instruction, so no synthetic SystemMessage has to be persisted
into the session history.
"""

from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model
from app.graph.nodes._shared import complete_without_tools, role_for
from app.graph.state import IPAState
from app.prompts.assistant import FINALIZE_ADDENDUM


async def finalize_node(state: IPAState, config: RunnableConfig) -> dict:
    """Produce a user-facing answer without tools after the cap.

    Args:
        state: The current IPA state, including the persisted message history.
        config: The run configuration carrying callbacks and metadata.

    Returns:
        A state update containing the wrap-up message.
    """
    model = get_chat_model(role_for(state["channel"]))
    response = await complete_without_tools(
        model,
        state["messages"],
        lang=state.get("lang"),
        channel=state["channel"],
        addendum=FINALIZE_ADDENDUM,
        config=config,
    )
    return {"messages": [response]}
