"""HTTP + WebSocket endpoints for the assistant (M09 review).

Error-handling policy:
  - WebSocketDisconnect is the only exception we catch in the streaming loop.
    It means "the client left"; there is nothing useful to do.
  - Any other exception is unexpected / a bug. We deliberately let it
    propagate so Starlette closes the socket with code 1011 and the server
    logs the real traceback. We never use `except Exception` to convert
    unknown errors into a friendly JSON message.
"""

import asyncio
import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from langchain_core.messages import AIMessageChunk

from app.api.deps import build_initial_state, build_run_config, get_graph
from app.api.schemas import ChatRequest, ChatResponse
from app.graph.utils import get_last_message

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for the Docker HEALTHCHECK.

    Returns:
        A dictionary indicating the health status.
    """
    return {"status": "ok"}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    graph: Any = Depends(get_graph),  # noqa: B008
) -> ChatResponse:
    """One-shot request/response: run the graph, return the final answer.

    Args:
        payload: The request body containing the chat message and session ID.
        graph: The graph instance to use for processing the request.

    Returns:
        A ChatResponse object containing the assistant's reply, resolved language,
        and routed intent.
    """
    initial = build_initial_state(
        user_input=payload.message,
        session_id=payload.session_id,
        channel="api",
        device_id=payload.device_id,
    )
    config = build_run_config(payload.session_id, channel="api")

    # Run the graph in a thread to avoid blocking the event loop,
    # with a hard deadline so a hung model can't hold a thread forever.
    try:
        async with asyncio.timeout(90):
            final = await asyncio.to_thread(graph.invoke, initial, config=config)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="The assistant took too long. Please try again.",
        )

    last = get_last_message(final["messages"])

    return ChatResponse(
        reply=_content_to_str(last.content),
        lang=final.get("lang"),
        route=final.get("route"),
    )


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    """Token streaming over WebSocket via graph.astream_events(version="v2").

    Args:
        websocket: FastAPI WebSocket object for bi-directional communication.

    Return:
        None. The function handles the WebSocket connection and streams tokens
        to the client until the conversation is complete or the client disconnects.
    """
    graph = websocket.app.state.graph

    # Accept the WebSocket connection before receiving any messages.
    await websocket.accept()

    try:
        # Receive the initial JSON payload from the client and validate it against
        # the ChatRequest schema.
        raw = await websocket.receive_json()
        payload = ChatRequest.model_validate(raw)

    except WebSocketDisconnect:
        # The client disconnected before sending any data.
        # We simply return and do not attempt to send any response.
        return
    except ValueError as exc:
        # receive_json() raises json.JSONDecodeError for malformed JSON, and
        # model_validate raises pydantic ValidationError — both are ValueError
        # subclasses, so one branch covers "I got data I can't use".
        try:
            await websocket.send_json(
                {"type": "error", "detail": f"invalid payload: {exc}"}
            )
            await websocket.close(code=1003)  # 1003 = Unsupported Data
        except WebSocketDisconnect:
            pass
        return

    initial = build_initial_state(
        user_input=payload.message,
        session_id=payload.session_id,
        channel="api",
        device_id=payload.device_id,
    )
    config = build_run_config(payload.session_id, channel="api")

    streamed: list[str] = []
    final_state: dict[str, Any] | None = None
    thinking_sent = False  # send the router status exactly once
    completed = False  # True only when the root on_chain_end is seen

    try:
        # Stream events from the graph and send them to the client in real-time.
        async with asyncio.timeout(90):
            async for event in graph.astream_events(
                initial, config=config, version="v2"
            ):
                kind = event["event"]

                # The graph emits "on_chat_model_stream" events for each token chunk
                # produced by the model.
                if kind == "on_chat_model_stream":
                    node = event.get("metadata", {}).get("langgraph_node")
                    if node == "responder":
                        chunk = event["data"]["chunk"]
                        if isinstance(chunk, AIMessageChunk):
                            text = _content_to_str(chunk.content)
                            if text:
                                streamed.append(text)
                                await websocket.send_json(
                                    {"type": "token", "content": text}
                                )

                # Status frame when the router's model call begins
                if kind == "on_chat_model_start":
                    node = event.get("metadata", {}).get("langgraph_node")
                    if node == "router" and not thinking_sent:
                        thinking_sent = True
                        await websocket.send_json(
                            {"type": "status", "detail": "classifying intent..."}
                        )

                if (
                    kind == "on_chain_end"
                    and event.get("name") == "LangGraph"
                    and not event.get("parent_ids")
                ):
                    final_state = event["data"]["output"]
                    completed = True

    # We only catch WebSocketDisconnect here because it indicates that the client has disconnected.
    except WebSocketDisconnect:
        return

    except TimeoutError:
        # 1013 = "Try Again Later": a transient server-side condition, not a bug.
        try:
            await websocket.send_json({"type": "error", "detail": "timeout"})
            await websocket.close(code=1013)
        except WebSocketDisconnect:
            pass  # client left while we were reporting the timeout
        return

    except Exception:
        # Preserves your policy: never swallow the error. Log it, tell the
        # client best-effort, then re-raise so Starlette closes with 1011.
        logger.exception("graph run failed mid-stream")
        try:
            await websocket.send_json(
                {"type": "error", "detail": "assistant failed mid-stream"}
            )
        except WebSocketDisconnect:
            pass
        raise

    reply = "".join(streamed)
    # If the graph didn't produce any tokens but did produce a final state,
    # extract the last message from the final state to form the reply.

    if not reply and final_state is not None:
        reply = _content_to_str(get_last_message(final_state["messages"]).content)

    # TODO: Your handler reads only one incoming message, runs the graph, then closes.
    # That's fine for "one turn per connection," but a chat UI usually keeps the
    # socket open for multi-turn. Just be deliberate about which model you're building.
    # Suggestion: defer this TODO until persistence exists.

    try:
        # Send a final JSON message indicating the end of the conversation, along with
        # the assistant's reply, resolved language, routed intent, and completion status.
        await websocket.send_json(
            {
                "type": "done",
                "reply": reply,
                "lang": final_state.get("lang") if final_state else None,
                "route": final_state.get("route") if final_state else None,
                "complete": completed,
            }
        )
    except WebSocketDisconnect:
        pass  # client left before receiving the terminal event
    finally:
        await websocket.close()


def _content_to_str(content: Any) -> str:
    """Normalize AIMessage.content (str | list of content blocks) to text.

    Args:
        content: The content of an AIMessage, which can be a string or a list of content blocks.

    Returns:
        A string representation of the content, concatenating text from content blocks if necessary.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)
