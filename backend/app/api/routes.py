"""HTTP + WebSocket endpoints for the assistant (M09, checkpointing in M11).

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
from langchain_core.messages import AIMessageChunk, BaseMessage

from app._version import __version__
from app.api.auth import authorize_ws, require_api_key
from app.api.deps import (
    build_initial_state,
    build_run_config,
    check_ws_rate_limit,
    get_checkpointer,
    get_graph,
    require_rate_limit,
)
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    HistoryMessage,
    SessionHistoryResponse,
)
from app.core.config import settings
from app.core.observability import enrich_trace, turn_span
from app.graph.utils import get_last_message

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for the Docker HEALTHCHECK.

    Returns:
        A dictionary with the health status and the running app version.
    """
    return {"status": "ok", "version": __version__}


@router.post(
    "/chat",
    response_model=ChatResponse,
    dependencies=[Depends(require_api_key), Depends(require_rate_limit)],
)
async def chat(
    payload: ChatRequest,
    graph: Any = Depends(get_graph),  # noqa: B008
) -> ChatResponse:
    """One-shot request/response: run the graph, return the final answer.

    M11: we call ``graph.ainvoke`` (async), matching the async checkpointer.
    LangGraph runs the sync node code in a thread executor internally, so the
    event loop is not blocked. Because ``build_run_config`` stamps the
    session's thread_id into the config, this turn is MERGED into the saved
    history for that session instead of starting from scratch.

    Args:
        payload: The request body containing the chat message and session ID.
        graph: The graph instance to use for processing the request.

    Returns:
        A ChatResponse object containing the assistant's reply, resolved
        language, and routed intent.
    """
    initial = build_initial_state(
        user_input=payload.message,
        session_id=payload.session_id,
        channel="api",
        device_id=payload.device_id,
    )
    config = build_run_config(payload.session_id, channel="api")

    # Build_run_config has stamped session/channel onto the log
    # context, so the lines below (and any logger inside the run) now carry
    # the correlation id.
    logger.info("chat run start")

    # Cooperative cancellation, NOT an abort. asyncio.timeout only
    # cancels THIS coroutine's await. LangGraph runs its sync node code — the
    # DeepSeek HTTP call included — on a thread-executor thread, so firing
    # this timeout releases the HTTP request but does NOT stop the in-flight
    # model call: it keeps occupying its thread until it finishes or hits its
    # own bound. The REAL per-call bound is ROLE_CONFIG[role]["timeout"]
    # (app/core/llm.py), enforced by the ChatDeepSeek client; this deadline is
    # only a coarse request-level safety net.
    try:
        async with asyncio.timeout(settings.graph_timeout_seconds):
            # turn_span() owns the app-root span, so enrich_trace() has a
            # recording span to write the trace name/tags/metadata onto.
            with turn_span():
                # The graph.ainvoke method runs the graph with the initial state and configuration.
                # it must wait for the async graph to complete and return the final state (async
                # like the checkpointer).
                final = await graph.ainvoke(initial, config=config)
                enrich_trace(final)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="The assistant took too long. Please try again.",
        )

    # Closing correlation line for this run — both route/lang land in
    # the JSON/text record alongside session_id.
    logger.info(
        "chat run complete (route=%s lang=%s)", final.get("route"), final.get("lang")
    )

    last = get_last_message(final["messages"])

    return ChatResponse(
        reply=_content_to_str(last.content),
        lang=final.get("lang"),
        route=final.get("route"),
    )


@router.get(
    "/sessions/{session_id}/history",
    response_model=SessionHistoryResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_session_history(
    session_id: str,
    checkpointer: Any = Depends(get_checkpointer),  # noqa: B008
) -> SessionHistoryResponse:
    """Return every persisted message for a session, oldest first.

    Reads the LATEST checkpoint for the thread straight from SQLite. This is
    a debugging/inspection endpoint — the chat routes never call it; they use
    the same checkpointer implicitly through graph.ainvoke/astream_events.

    Args:
        session_id: The thread_id that ties turns into one conversation.
        checkpointer: The app-wide AsyncSqliteSaver (from startup).

    Returns:
        The session's full message history (empty if the session never ran).
    """
    checkpoint_tuple = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": session_id}}
    )
    if checkpoint_tuple is None:
        return SessionHistoryResponse(session_id=session_id, messages=[])

    state = checkpoint_tuple.checkpoint["channel_values"]
    messages = state.get("messages", [])
    return SessionHistoryResponse(
        session_id=session_id,
        messages=[_message_to_history(m) for m in messages],
    )


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(require_api_key)],
)
async def delete_session(
    session_id: str,
    checkpointer: Any = Depends(get_checkpointer),  # noqa: B008
) -> dict[str, Any]:
    """Delete a conversation and all its checkpoints.

    Calls ``checkpointer.adelete_thread``, which removes every checkpoint AND
    pending write for that thread_id from the SQLite store. Deleting a thread
    that never existed is a harmless no-op (zero rows removed), so we don't
    bother returning a 404.

    Args:
        session_id: The thread_id of the conversation to delete.
        checkpointer: The app-wide AsyncSqliteSaver (from startup).

    Returns:
        A small confirmation dict for the client.
    """
    await checkpointer.adelete_thread(session_id)
    return {"session_id": session_id, "deleted": True}


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

    # Resolve the presented key BEFORE accepting. The client offers the
    # API key as a Sec-WebSocket-Protocol (subprotocol) token — browsers
    # cannot set headers on a WS handshake but CAN pass subprotocols — with
    # ?api_key= kept as a documented fallback. authorize_ws returns the
    # subprotocol the server must echo: RFC 6455 requires the server to select
    # exactly one of the client's offered subprotocols, otherwise the browser
    # aborts the handshake. So the choice has to be made pre-accept.
    authorized, subprotocol = authorize_ws(websocket)
    await websocket.accept(subprotocol=subprotocol)

    if not authorized:
        # Never log the key itself (BE-09) — only the transport that was used.
        logger.warning(
            "ws auth rejected (transport=%s)",
            "subprotocol" if subprotocol else "query",
        )
        try:
            await websocket.close(code=1008)  # 1008 = Policy Violation
        except WebSocketDisconnect:
            pass  # client vanished while we were rejecting them
        return

    # Per-key rate limit for sockets too (same budget as HTTP — both count
    # against the presented API key). The key is the subprotocol token when
    # that transport was used, else the ?api_key= query value.
    presented_key = subprotocol or websocket.query_params.get("api_key")
    if not check_ws_rate_limit(websocket, presented_key):
        logger.warning("ws rate limit exceeded")
        try:
            await websocket.send_json(
                {"type": "error", "detail": "rate limit exceeded"}
            )
            await websocket.close(code=1013)  # 1013 = Try Again Later
        except WebSocketDisconnect:
            pass  # client vanished while we were reporting the limit
        return

    # BE-09: a WS handler is ONE long-lived task, so log the transport (never
    # the key). The correlation session_id is stamped by build_run_config once
    # the payload below yields a session_id, so later lines carry it too.
    logger.info(
        "ws chat connected (transport=%s)",
        "subprotocol" if subprotocol else "query",
    )

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
    # Build the run configuration with the session ID and channel for telemetry and checkpointing.
    config = build_run_config(payload.session_id, channel="api")

    streamed: list[str] = []
    final_state: dict[str, Any] | None = None
    thinking_sent = False  # send the router status exactly once
    completed = False  # True only when the root on_chain_end is seen

    try:
        # Same cooperative-cancellation caveat as POST /chat: this
        # timeout cancels the stream's await, not the in-flight model call on
        # its thread-executor thread. ROLE_CONFIG's per-role timeout is the
        # real bound on each DeepSeek call.
        async with asyncio.timeout(settings.graph_timeout_seconds):
            # turn_span() must wrap the WHOLE stream: our span has to exist
            # before the callback handler creates the run's root chain span.
            with turn_span():
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

                # The stream is over and no callback span is active any more:
                # stamp the trace while our app-root span is still open. This
                # sits OUTSIDE the loop on purpose — one enrichment per run.
                if final_state is not None:
                    enrich_trace(final_state)

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


def _message_to_history(message: BaseMessage) -> HistoryMessage:
    """Flatten a BaseMessage into a HistoryMessage for the API response.

    Args:
        message: Any LangChain message (human/ai/tool/system).

    Returns:
        A HistoryMessage with "role" (message.type) and flattened text content.
    """
    return HistoryMessage(role=message.type, content=_content_to_str(message.content))
