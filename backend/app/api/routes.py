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
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessageChunk

from app.api.deps import build_initial_state, build_run_config
from app.api.schemas import ChatRequest, ChatResponse
from app.graph.utils import get_last_message

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for the Docker HEALTHCHECK.

    Returns:
        A dictionary indicating the health status.
    """
    return {"status": "ok"}


# TODO: Is Depends(get_graph) the right way to get the graph here?
# Or is request.app.state.graph better?
@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    """One-shot request/response: run the graph, return the final answer.

    Args:
        request: FastAPI Request object, which has a reference to the app.
        payload: The request body containing the chat message and session ID.

    Returns:
        A ChatResponse object containing the assistant's reply, resolved language,
        and routed intent.
    """
    # TODO: should I use get_graph instead?
    graph = request.app.state.graph

    initial = build_initial_state(
        user_input=payload.message,
        session_id=payload.session_id,
        channel="api",
        device_id=payload.device_id,
    )
    config = build_run_config(payload.session_id, channel="api")

    # Run the graph in a thread to avoid blocking the event loop.
    # TODO: add a route-level safety net in chat() using asyncio.timeout(90) around
    # the to_thread call and map TimeoutError to a 503. This is the single most
    # valuable production habit for LLM serving.
    final = await asyncio.to_thread(graph.invoke, initial, config=config)

    last = get_last_message(final["messages"])

    return ChatResponse(
        reply=_content_to_str(last.content),
        lang=final.get("lang"),
        route=final.get("route"),
    )


# TODO: Add a per-request deadline: use asyncio.timeout(...) in the WS handler too
# (not just POST /chat), and on timeout send {"type": "error", "detail": "timeout"}
# then close with code 1013 ("try again later") instead of 1011. Read what
# code 1011 vs 1013 mean to clients — that's the "correct" part.


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
        # TODO: One improvement you could make: send {"type": "error", "detail": "invalid payload"}
        # back before returning, so a misbehaving client learns why it got closed.
        raw = await websocket.receive_json()
        payload = ChatRequest.model_validate(raw)
    except (WebSocketDisconnect, ValueError):
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

    try:
        # Stream events from the graph and send them to the client in real-time.
        # TODO: What does v2 means? Is it lib related or has anything to do with my code release version?
        async for event in graph.astream_events(initial, config=config, version="v2"):
            kind = event["event"]

            # TODO: Where does on_chat_model_stream come from? Is it a langchain event
            # or something I defined in my code?
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

            # TODO: Add a "thinking" frame: for on_chat_model_stream events where
            # langgraph_node == "router", send {"type": "status", "detail": "classifying intent..."}
            # (don't leak the raw tokens — just a status). Re-run ws_probe.py and watch the
            # ordering of status → token → done.

            if (
                kind == "on_chain_end"
                and event.get("name") == "LangGraph"
                and not event.get("parent_ids")
            ):
                final_state = event["data"]["output"]

    # We only catch WebSocketDisconnect here because it indicates that the client has disconnected.
    except WebSocketDisconnect:
        return

    reply = "".join(streamed)
    # If the graph didn't produce any tokens but did produce a final state,
    # extract the last message from the final state to form the reply.

    if not reply and final_state is not None:
        reply = _content_to_str(get_last_message(final_state["messages"]).content)

    # TODO: The fallback if not reply and final_state is not None handles the case where
    # the graph produced a final state but zero tokens (e.g., a model returned empty).
    # But there's no branch for partial tokens + a later failure — the done frame would
    # carry an incomplete reply. Consider tracking whether the run ended cleanly.

    # TODO: Your handler reads only one incoming message, runs the graph, then closes.
    # That's fine for "one turn per connection," but a chat UI usually keeps the
    # socket open for multi-turn. Just be deliberate about which model you're building.

    try:
        # Send a final JSON message indicating the end of the conversation, along with
        # the assistant's reply, resolved language, and routed intent.
        await websocket.send_json(
            {
                "type": "done",
                "reply": reply,
                "lang": final_state.get("lang") if final_state else None,
                "route": final_state.get("route") if final_state else None,
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
