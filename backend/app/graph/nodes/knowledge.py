"""knowledge_node — a specialist bound to the tool registry."""

from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model
from app.graph.state import IPAState
from app.tools.registry import TOOLS


def knowledge_node(state: IPAState, config: RunnableConfig) -> dict:
    """Function to handle knowledge requests by invoking a specialist model bound to the calculator tool.

    Args:
        state (IPAState): The current state of the IPA, including message history.
        config (RunnableConfig): The configuration for the runnable.

    Returns:
        dict: A dictionary containing the response from the knowledge specialist.
    """
    model = get_chat_model("specialist").bind_tools(TOOLS)
    response = model.invoke(state["messages"], config)
    return {"messages": [response]}
