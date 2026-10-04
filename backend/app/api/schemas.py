"""Pydantic v2 request/response models for the HTTP API (M09).

Two jobs:
  1. VALIDATION at the boundary — FastAPI parses incoming JSON into these
     and returns 422 for bad input BEFORE our code runs.
  2. DOCUMENTATION — the /docs page renders these as the request/response
     contract, so a consumer can read the API without reading code.

Pydantic v2 notes:
  - Field(...) with min_length/max_length does the validation.
  - `str | None = None` makes device_id optional (defaults to None).
  - Declaring a return type on the route (response_model) makes FastAPI
    SERIALIZE our object and document it — we never hand-roll JSON.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Body of POST /chat and the JSON envelope sent over /ws/chat."""

    session_id: str = Field(
        min_length=1,
        description="Stable id that groups turns into one conversation.",
    )
    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The user's turn, plain text.",
    )
    device_id: str | None = Field(
        default=None,
        description="Which device this turn came from (optional).",
    )


class ChatResponse(BaseModel):
    """Body of the POST /chat reply."""

    reply: str = Field(description="The assistant's final answer.")
    lang: str | None = Field(description="Resolved language tag, e.g. 'pt-BR'.")
    route: str | None = Field(description="Routed intent, e.g. 'knowledge'.")


class HistoryMessage(BaseModel):
    """One persisted message in a session's history."""

    role: str = Field(
        description="Message type: 'human', 'ai', 'tool' or 'system'.",
    )
    content: str = Field(description="Flattened text of the message.")


class SessionHistoryResponse(BaseModel):
    """Body of GET /sessions/{id}/history."""

    session_id: str = Field(description="The thread_id this history belongs to.")
    messages: list[HistoryMessage] = Field(
        description="All persisted messages for the session, oldest first.",
    )


class VoiceStartRequest(BaseModel):
    """Body of /ws/voice's first frame: which conversation, and the audio contract."""

    session_id: str = Field(
        min_length=1,
        description="Stable id that groups turns into one conversation.",
    )
    device_id: str | None = Field(
        default=None,
        description="Which device is speaking (optional).",
    )
    output_sample_rate: Literal[16000, 24000] = Field(
        default=24000,
        description=(
            "Rate for the reply audio: 24000 for a browser (TTS-native, no "
            "resampling), 16000 for the reSpeaker XVF3800. Kept in step with "
            "app.voice.session.OUTPUT_SAMPLE_RATES by a test."
        ),
    )
