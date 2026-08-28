"""ws_probe.py — tiny WebSocket client to WATCH token streaming (M09).

Why this exists: the /docs Swagger page cannot test WebSockets (it only
does HTTP), so this is your manual proof that tokens arrive one at a time.

Usage (from backend/, while the server is up):
    uv run python scripts/ws_probe.py "tell me a short joke"
"""

import asyncio
import json
import sys

import websockets  # comes with uvicorn[standard]


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/ws_probe.py '<your message>'")
        raise SystemExit(1)
    message = " ".join(sys.argv[1:])
    uri = "ws://localhost:8000/ws/chat"

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"session_id": "probe-1", "message": message}))
        while True:
            event = json.loads(await ws.recv())
            if event["type"] == "token":
                print(event["content"], end="", flush=True)
            elif event["type"] == "done":
                print()
                print("--- done ---")
                print(json.dumps(event, ensure_ascii=False))
                break
            elif event["type"] == "error":
                print("--- error ---", event["detail"])
                break


if __name__ == "__main__":
    asyncio.run(main())
