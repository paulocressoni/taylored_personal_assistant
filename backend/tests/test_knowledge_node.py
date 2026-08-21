"""knowledge_node with a faked model — the M06 'fake the LLM' showcase.

This node exercises the trickiest seam: it calls

    get_chat_model("specialist").bind_tools(TOOLS)

then invokes with the RAW message history (SystemMessage prepended locally).

Why a FakeChatModel and not the real model:
  - The real model needs an API key + network and is non-deterministic.
  - langchain-core's GenericFakeChatModel raises NotImplementedError on
    bind_tools() (verified), so we fake the two methods the node touches.
The fake RECORDS its inputs, so we can assert WHAT the node sent: that the
system prompt was prepended and that the real TOOLS were bound.
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, SystemMessage

from app.graph.nodes.knowledge import knowledge_node
from app.prompts.knowledge import KNOWLEDGE_SYSTEM_PROMPT
from app.tools.registry import TOOLS


def test_knowledge_node_binds_tools_and_prepends_system_prompt(patch_llm) -> None:
    fake = FakeChatModel(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "0.15 * 240"}, "id": "1"}
            ],
        )
    )
    patch_llm(lambda role: fake)

    state = make_state(user_input="What is 15% of 240?")
    result = knowledge_node(state, config=None)

    # 1. The node returned the model's message (a tool call) as its output.
    (out_msg,) = result["messages"]
    assert isinstance(out_msg, AIMessage)
    assert out_msg.tool_calls[0]["name"] == "calculate"

    # 2. bind_tools() was called with the real registry — proving wiring.
    assert fake.bound_tools == TOOLS

    # 3. The model received the SystemMessage first, then the history —
    #    and crucially the prompt is NOT persisted into state["messages"].
    sent = fake.calls[0]
    assert isinstance(sent[0], SystemMessage)
    assert KNOWLEDGE_SYSTEM_PROMPT in sent[0].content
    assert state["messages"] == []
