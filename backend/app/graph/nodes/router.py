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

from app.core.llm import get_chat_model
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


async def router_node(state: IPAState, config: RunnableConfig) -> dict:
    """Route the user input to one intent using the router prompt.

    The call is awaited instead of run in a worker thread so a request-level
    cancellation can stop the in-flight provider request.

    Args:
        state: The current IPA state, including the user input.
        config: The run configuration carrying callbacks and metadata.

    Returns:
        A state update containing the routed intent.
    """
    model = get_chat_model("router")
    messages = ROUTER_PROMPT.invoke({"user_input": state["user_input"]}).to_messages()
    response = await model.ainvoke(messages, config=config)

    # reply.content is typed as a union (str | content blocks). For a text
    # model it's always a str; anything else means "no usable tag" -> responder.
    content = response.content
    route = parse_route(content) if isinstance(content, str) else DEFAULT_ROUTE
    return {"route": route}
