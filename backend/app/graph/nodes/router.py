"""router_node — decide the intent and tag it with <route>...</route>.

Tag-based routing, NOT JSON/structured output. Why:
  - DeepSeek's strict-mode schema adherence "may be ignored" per its docs.
  - JSON mode can return empty content.
A regex over a <route>...</route> tag is deterministic and unit-testable
without mocking a JSON schema.
"""

import re

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model, llm_call
from app.graph.state import IPAState
from app.prompts.router import ROUTER_INSTRUCTIONS

VALID_ROUTES = {
    "knowledge",
    "time",
    "weather",
    "calendar",
    "music",
    "alarm",
    "clarify",
    "responder",
}
DEFAULT_ROUTE = "responder"

ROUTER_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", ROUTER_INSTRUCTIONS),
        ("human", "{user_input}"),
    ]
)


def parse_route(text: str) -> str:
    """Extract the route from a <route>...</route> tag. Never raises.

    Defaults to DEFAULT_ROUTE when the tag is missing, empty, or invalid.
    """
    match = re.search(r"<route>\s*([A-Za-z]+)\s*</route>", text or "")
    if not match:
        return DEFAULT_ROUTE
    candidate = match.group(1).lower()
    return candidate if candidate in VALID_ROUTES else DEFAULT_ROUTE


def router_node(state: IPAState, config: RunnableConfig) -> dict:
    """Function to route user input to the appropriate intent based on the ROUTER_PROMPT.

    Args:
        state (IPAState): The current state of the IPA, containing user input and other relevant information.
        config (RunnableConfig): The configuration for the runnable.

    Returns:
        dict: A dictionary containing the routed intent.
    """
    model = get_chat_model("router")
    messages = ROUTER_PROMPT.invoke({"user_input": state["user_input"]}).to_messages()
    llm_result = llm_call(state, model, messages, config)
    response = llm_result["response"]  # Extract the response from the llm_call result
    llm_calls = llm_result["llm_calls"]  # Extract the LLM call count

    # reply.content is typed as a union (str | content blocks). For a text
    # model it's always a str; anything else means "no usable tag" -> responder.
    content = response.content
    route = parse_route(content) if isinstance(content, str) else DEFAULT_ROUTE
    return {"route": route, "llm_calls": llm_calls}
