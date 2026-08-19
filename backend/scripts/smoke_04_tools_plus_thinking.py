"""smoke_04_tools_plus_thinking.py — tools AND thinking in one call.

This combination is UNDOCUMENTED. Whatever happens (works / errors / silently
drops tools / ignores thinking), record it in the README. The deliverable is
the observation, not a pass/fail.
"""

import pytest
from pydantic import BaseModel, Field

from app.core.llm import get_chat_model

pytestmark = pytest.mark.integration


class GetWeather(BaseModel):
    """Get the current weather in a city."""

    location: str = Field(..., description="City name, e.g. Berlin")


PROMPT = "What's the weather in Berlin? Use the provided tool."


def _run():
    llm = get_chat_model("specialist").bind_tools([GetWeather])
    return llm.invoke(PROMPT, extra_body={"thinking": {"type": "enabled"}})


def test_smoke_04_tools_plus_thinking():
    reply = _run()  # do not force an outcome — observe it
    print("tool_calls    :", reply.tool_calls)
    print(
        "reasoning     :",
        (reply.additional_kwargs.get("reasoning_content") or "")[:200],
    )
    print("content       :", reply.content)
    print("usage_metadata:", reply.usage_metadata)
    # Minimal sanity: the API responded with text and/or a tool call.
    assert reply.content or reply.tool_calls


if __name__ == "__main__":
    reply = _run()
    print("tool_calls    :", reply.tool_calls)
    print(
        "reasoning     :",
        (reply.additional_kwargs.get("reasoning_content") or "")[:200],
    )
    print("content       :", reply.content)
    print("usage_metadata:", reply.usage_metadata)
