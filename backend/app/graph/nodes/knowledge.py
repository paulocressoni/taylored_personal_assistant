"""knowledge_node — a specialist bound to the tool registry.

Unlike router/responder (which build input from state fields via a
ChatPromptTemplate), this node passes the RAW message history to the model so
tool calls and results flow through the ReAct loop. The system prompt is
prepended locally per call — never written into state["messages"], so it
doesn't leak into the responder or persisted history.
"""

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model, llm_call
from app.graph.state import IPAState
from app.prompts.knowledge import KNOWLEDGE_SYSTEM_PROMPT
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
    messages = [SystemMessage(content=KNOWLEDGE_SYSTEM_PROMPT), *state["messages"]]

    llm_response = llm_call(model, messages, config)
    response = llm_response["response"]  # Extract the response from the llm_call result

    return {"messages": [response]}
