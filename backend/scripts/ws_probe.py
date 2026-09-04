"""ws_probe.py — tiny WebSocket client to WATCH token streaming (M09).

Why this exists: the /docs Swagger page cannot test WebSockets (it only
does HTTP), so this is your manual proof that tokens arrive one at a time.

The server rejects unauthenticated sockets (close code 1008), so the probe
reads the shared API key from app.core.config.settings and sends it as
?api_key= on the URI automatically — no manual key needed.

Usage (from backend/, while the server is up):
    uv run python scripts/ws_probe.py "tell me a short joke"
"""

import asyncio
import json
import os
import sys
from urllib.parse import urlencode

import websockets  # comes with uvicorn[standard]


def _shared_api_key() -> str:
    """Return the shared API key the server enforces on the WS handshake.

    Importing app.core.config triggers fail-fast ENV validation, so default
    ENV to dev here (this probe only ever targets the local dev server)
    before reading ``settings.assistant_api_key``.
    """
    os.environ.setdefault("ENV", "dev")
    from app.core.config import settings

    return settings.assistant_api_key.get_secret_value()


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/ws_probe.py '<your message>'")
        raise SystemExit(1)
    message = " ".join(sys.argv[1:])
    # ?api_key= is a query param (not a header) because WS clients cannot set
    # headers on the handshake. urlencode URL-encodes the key value.
    uri = f"ws://localhost:8000/ws/chat?{urlencode({'api_key': _shared_api_key()})}"

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"session_id": "probe-1", "message": message}))
        while True:
            # Wait for events from the server and print them to stdout.
            event = json.loads(await ws.recv())

            # Print status updates
            if event["type"] == "status":
                print(event["detail"], end="", flush=True)
                print()  # newline after the status line

            # Print token chunks
            elif event["type"] == "token":
                print(event["content"], end="", flush=True)

            # Print the final "done" event and exit
            elif event["type"] == "done":
                print()
                print("--- done ---")
                print(json.dumps(event, ensure_ascii=False))
                break

            # Handle any error events and exit
            elif event["type"] == "error":
                print("--- error ---", event["detail"])
                break


if __name__ == "__main__":
    asyncio.run(main())
