"""Shared-API-key authentication for HTTP and WebSocket routes.

One key from Settings, checked two ways because the transports differ:
  - HTTP: the client sends it as the X-API-Key header.
  - WebSocket: browsers cannot set headers on a WS handshake, so the client
    sends it as the ?api_key= query parameter instead.

Separate module on purpose: routes.py stays thin and this policy is testable
in isolation.
"""

import secrets

from fastapi import Header, HTTPException, WebSocket, status

from app.core.config import settings


def _key_matches(candidate: str | None) -> bool:
    """Compare in constant time (no length-based early exit)."""
    expected = settings.assistant_api_key.get_secret_value()
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency for HTTP routes: 401 unless the key matches."""
    if not _key_matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_ws_api_key(websocket: WebSocket) -> bool:
    """Validate a WS connection's ?api_key= query param.

    Returns True when authorized. The route must accept() the socket FIRST and
    only then close with code 1008 when this is False — a close frame cannot be
    sent before the handshake is accepted.
    """
    return _key_matches(websocket.query_params.get("api_key"))
