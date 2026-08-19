"""responder_node — the LLM that answers the user.

It only ever returns ONE message. The reducer on `messages` is what
turns that into "one more message appended to history".
"""

from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model
from app.graph.state import IPAState


def responder_node(state: IPAState, config: RunnableConfig) -> dict:
    model = get_chat_model("responder")

    # state["messages"] already contains the full history (reducer kept it).
    response = model.invoke(state["messages"], config)

    # Returning a 1-element list: reducer appends it. No reducer -> clobbers.
    return {"messages": [response]}
