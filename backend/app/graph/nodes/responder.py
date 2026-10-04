"""responder_node — the LLM that turns the conversation into the final answer.

Builds its input with a ChatPromptTemplate + MessagesPlaceholder: a templated
system message (persona, language) with the full history inserted. Unlike the
knowledge node (raw-history pass-through for the ReAct loop), the responder is
terminal, so a template is the right shape here.
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.core.llm import get_chat_model, llm_call
from app.graph.state import IPAState
from app.graph.utils import trim_messages
from app.language.detector import DEFAULT_LANG
from app.prompts.responder import RESPONDER_SYSTEM_PROMPT, VOICE_STYLE_ADDENDUM


def _prompt(system_prompt: str) -> ChatPromptTemplate:
    """Build a responder template around a given system prompt."""
    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )


RESPONDER_PROMPT = _prompt(RESPONDER_SYSTEM_PROMPT)

# Voice turns get the SAME persona and rules plus a spoken-answer addendum:
# the base prompt is tuned for a reply that is read, the addendum for one that
# is heard. Appending keeps the base text a shared prefix, which is what the
# provider's prompt cache keys on, and puts the brevity rules LAST — the
# position with the strongest pull on the model.
VOICE_PROMPT = _prompt(f"{RESPONDER_SYSTEM_PROMPT.rstrip()}\n\n{VOICE_STYLE_ADDENDUM}")


def responder_node(state: IPAState, config: RunnableConfig) -> dict:
    """Generate the final user-facing answer with the responder model.

    The model only ever sees the CAPPED history window: we trim
    state["messages"] to Settings.max_history_messages so long sessions
    cannot exceed the model's context window. The persisted state is left
    untouched — only what the model sees is trimmed.

    A `channel` of "voice" selects the spoken-answer addendum: the same
    persona and rules, plus brevity and "no markdown" instructions, because
    every character of a voice reply is read aloud.

    Args:
        state: The current state of the IPA, including message history and
            language preference.
        config: The configuration for the runnable.

    Returns:
        A dictionary containing the final user-facing response message.
    """
    model = get_chat_model("responder")
    prompt = VOICE_PROMPT if state["channel"] == "voice" else RESPONDER_PROMPT
    # Cap the window handed to the model; state["messages"] (the
    # checkpointer's full session memory) is never mutated.
    history = trim_messages(state["messages"], settings.max_history_messages)
    messages = prompt.invoke(
        {
            "lang": state.get("lang") or DEFAULT_LANG,
            "messages": history,
        }
    ).to_messages()
    llm_response = llm_call(model, messages, config)
    response = llm_response["response"]  # Extract the response from the llm_call result
    return {"messages": [response]}
