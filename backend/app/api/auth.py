"""Shared-API-key authentication for HTTP and WebSocket routes.

One key from Settings, checked per transport:
  - HTTP: the client sends it as the X-API-Key header.
  - WebSocket: the key travels as a Sec-WebSocket-Protocol (subprotocol)
    token — browsers cannot set headers on a WS handshake, but they CAN pass
    subprotocols. ``?api_key=`` stays as a documented fallback for
    clients that cannot use a subprotocol (e.g. the bundled ws_probe.py).

Keys are compared in constant time (secrets.compare_digest), so timing
attacks can't leak the value. This module stays separate so routes.py is thin
and the policy is testable in isolation.
"""

import secrets

from fastapi import Header, HTTPException, WebSocket, status

from app.core.config import settings


def _key_matches(candidate: str | None) -> bool:
    """Compare a presented key to the expected one in constant time.

    Args:
        candidate: The key the client presented (None if absent).

    Returns:
        True when ``candidate`` equals the configured ``ASSISTANT_API_KEY``.
    """
    expected = settings.assistant_api_key.get_secret_value()
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency for HTTP routes: 401 unless the key matches.

    Args:
        x_api_key: The X-API-Key header value.

    Raises:
        HTTPException: 401 Unauthorized when the key is missing or wrong.
    """
    if not _key_matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def ws_offered_key(websocket: WebSocket) -> str | None:
    """The first subprotocol token the client offered, if any.

    A browser sends each requested subprotocol as a ``Sec-WebSocket-Protocol``
    header value; when several are offered the header is comma-separated. Our
    client offers exactly ONE token: the API key itself.

    Args:
        websocket: The (not yet accepted) WebSocket handshake.

    Returns:
        The first offered subprotocol token, or None if none was offered.
    """
    header = websocket.headers.get("sec-websocket-protocol")
    if not header:
        return None
    first = header.split(",")[0].strip()
    return first or None


def authorize_ws(websocket: WebSocket) -> tuple[bool, str | None]:
    """Authorize a WS handshake from a subprotocol token OR ``?api_key=``.

    The key no longer has to live in the URL. Offered as the WebSocket
    subprotocol it appears only in the (TLS-protected) handshake, never in
    access logs, proxy logs, or browser history. The query param stays as a
    documented fallback.

    Args:
        websocket: The WebSocket handshake being authorized.

    Returns:
        A tuple ``(authorized, subprotocol)``:
          - ``authorized``: True when the presented key matches
            ``ASSISTANT_API_KEY``.
          - ``subprotocol``: the token the route MUST echo in
            ``accept(subprotocol=...)`` when the key arrived as a subprotocol
            (None otherwise). RFC 6455 requires the server to echo exactly one
            of the client's offered subprotocols, or the browser aborts the
            handshake.
    """
    offered = ws_offered_key(websocket)
    if offered is not None and _key_matches(offered):
        return True, offered
    return _key_matches(websocket.query_params.get("api_key")), None
