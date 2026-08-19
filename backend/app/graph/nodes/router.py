"""router_node — decide the intent and tag it with <route>...</route>.

Tag-based routing, NOT JSON/structured output. Why:
  - DeepSeek's strict-mode schema adherence "may be ignored" per its docs.
  - JSON mode can return empty content.
A regex over a <route>...</route> tag is deterministic and unit-testable
without mocking a JSON schema.
"""

import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.core.llm import get_chat_model
from app.graph.state import IPAState

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

SYSTEM_PROMPT = """You are the intent router for a smart home assistant.
Read the user's message and pick EXACTLY ONE intent from this list:
knowledge, time, weather, calendar, music, alarm, clarify, responder.

Respond with ONLY a <route> tag, nothing else.

Examples:
User: what's 15% of 240
<route>knowledge</route>

User: what time is it in Tokyo
<route>time</route>

User: set an alarm for 7am
<route>alarm</route>

User: hello, how are you?
<route>responder</route>

Rules:
- "knowledge" = factual questions or calculations.
- If unsure, use "responder".
- Output nothing but the <route> tag.
"""


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
    """Function to route user input to the appropriate intent based on the SYSTEM_PROMPT.

    Args:
        state (IPAState): The current state of the IPA, containing user input and other relevant information.
        config (RunnableConfig): The configuration for the runnable.

    Returns:
        dict: A dictionary containing the routed intent.
    """
    model = get_chat_model("router")
    prompt = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=state["user_input"]),
    ]
    reply = model.invoke(prompt, config)
    return {"route": parse_route(reply.content)}
