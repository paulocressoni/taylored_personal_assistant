"""smoke_02_tools.py — does bind_tools() produce a non-empty tool_calls list?"""

import pytest
from app.core.llm import get_chat_model
from pydantic import BaseModel, Field

pytestmark = pytest.mark.integration


class GetWeather(BaseModel):
    """Get the current weather in a city."""

    location: str = Field(..., description="City name, e.g. Berlin")


PROMPT = "What's the weather in Berlin? Use the provided tool."


def _run():
    llm = get_chat_model("specialist").bind_tools([GetWeather])
    return llm.invoke(PROMPT)


def test_smoke_02_tools():
    reply = _run()
    assert reply.tool_calls, "expected the model to emit a tool call"
    print("tool_calls    :", reply.tool_calls)
    print("usage_metadata:", reply.usage_metadata)


if __name__ == "__main__":
    reply = _run()
    print("tool_calls    :", reply.tool_calls)
    print("usage_metadata:", reply.usage_metadata)
