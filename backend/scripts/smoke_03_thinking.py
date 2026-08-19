"""smoke_03_thinking.py — does thinking mode populate reasoning_content?

Probes both directions: enabled must populate reasoning_content, disabled
must clear it. Per-call extra_body overrides the factory's role default.
"""

import pytest
from app.core.llm import get_chat_model
from langchain_core.messages import AIMessage

pytestmark = pytest.mark.integration

PROMPT = "How many r's are in the word strawberry? Think step by step."


def _run(enabled: bool) -> AIMessage:
    llm = get_chat_model("specialist")
    thinking_type = "enabled" if enabled else "disabled"
    return llm.invoke(PROMPT, extra_body={"thinking": {"type": thinking_type}})


def test_smoke_03_thinking():
    """Enabled mode must populate reasoning_content; disabled must not."""
    on = _run(enabled=True)
    off = _run(enabled=False)
    assert on.additional_kwargs.get("reasoning_content"), (
        "enabled: no reasoning_content"
    )
    assert not off.additional_kwargs.get("reasoning_content"), (
        "disabled: reasoning still present"
    )
    print("enabled  reasoning:", on.additional_kwargs["reasoning_content"][:200])
    print("enabled  usage    :", on.usage_metadata)
    print("disabled reasoning:", off.additional_kwargs.get("reasoning_content"))
    print("disabled usage    :", off.usage_metadata)


if __name__ == "__main__":
    for enabled in (True, False):
        reply = _run(enabled)
        tag = "enabled " if enabled else "disabled"
        reasoning = reply.additional_kwargs.get("reasoning_content") or ""
        print(f"{tag} reasoning (first 200):", reasoning[:200])
        print(f"{tag} content            :", reply.content)
        print(f"{tag} usage_metadata     :", reply.usage_metadata)
