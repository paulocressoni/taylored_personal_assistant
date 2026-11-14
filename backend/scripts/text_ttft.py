"""text_ttft.py — measure text first-token latency through the real graph.

The WebSocket route forwards tokens as the graph produces them, so the
server-side time-to-first-token is what a text client actually waits through.
This script runs N turns through ``astream_events`` and reports p50/p90 from
run start to the first chunk emitted by an answer node.

No socket is involved: this isolates the graph from transport and browser
noise. The answer-node set covers the current ``responder`` and the planned
``assistant``/``finalize`` nodes, so the same probe works across the
single-assistant restructure.

Usage (from backend/, with the keys in .env.dev):
    ENV=dev uv run python scripts/text_ttft.py
    ENV=dev uv run python scripts/text_ttft.py --runs 5 --warmup 1
"""

import argparse
import asyncio
import math
import time
import uuid

from app.api.deps import build_initial_state, build_run_config
from app.graph.graph import build_graph

# Kept local until the planned app/graph/constants.py lands in the graph
# collapse; both names must be accepted so the probe spans that change.
ANSWER_NODES = frozenset({"responder", "assistant", "finalize"})


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile of ``values``.

    Args:
        values: Measured latencies in milliseconds.
        fraction: 0.50 for p50, 0.90 for p90.

    Returns:
        The requested percentile.
    """
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


async def _measure(graph: object, user_input: str) -> float:
    """Run one turn and return the first answer-token latency in milliseconds.

    Args:
        graph: The compiled graph returned by ``build_graph``.
        user_input: The message to send.

    Returns:
        Milliseconds from run start to the first answer-node token.

    Raises:
        RuntimeError: if the run produced no answer token.
    """
    session_id = uuid.uuid4().hex
    initial = build_initial_state(
        user_input=user_input,
        session_id=session_id,
        channel="api",
        device_id=None,
    )
    config = build_run_config(session_id, channel="api")
    started = time.perf_counter()

    async for event in graph.astream_events(initial, config=config, version="v2"):
        if event["event"] != "on_chat_model_stream":
            continue
        if event.get("metadata", {}).get("langgraph_node") not in ANSWER_NODES:
            continue
        return (time.perf_counter() - started) * 1000.0

    raise RuntimeError("the run produced no answer token")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="What is the capital of France?",
        help="message to send through the graph",
    )
    parser.add_argument("--runs", type=int, default=3, help="measured runs")
    parser.add_argument("--warmup", type=int, default=1, help="discarded warm-up runs")
    args = parser.parse_args()

    graph = build_graph()
    print(f"input    : {args.input!r}")
    print(f"schedule : {args.runs + args.warmup} runs, first {args.warmup} discarded")

    measured: list[float] = []
    for index in range(args.runs + args.warmup):
        elapsed_ms = await _measure(graph, args.input)
        label = "warm-up, discarded" if index < args.warmup else ""
        print(f"  run {index + 1:>2}: {elapsed_ms:8.1f} ms  {label}")
        if index >= args.warmup:
            measured.append(elapsed_ms)

    print()
    print(f"text first-token p50 = {_percentile(measured, 0.50):.1f} ms")
    print(f"text first-token p90 = {_percentile(measured, 0.90):.1f} ms")


if __name__ == "__main__":
    asyncio.run(_main())
