"""LLM client factory."""

from langchain_deepseek import ChatDeepSeek

from app.core.config import settings

### for now just testing LLM client access
llm = ChatDeepSeek(
    model="deepseek-v4-flash",
    api_key=settings.deepseek_api_key or None,
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    # other params...
)

messages = [
    (
        "system",
        "You are a helpful assistant that translates English to French. Translate the user sentence.",
    ),
    ("human", "I love programming."),
]
ai_msg = llm.invoke(messages)
print(ai_msg.content)
