from __future__ import annotations

import json
import threading
import time

import pytest

graphblocks_runtime = pytest.importorskip("graphblocks_runtime")


def _large_graph_json(node_count: int) -> str:
    nodes: dict[str, object] = {"n0": {"block": "custom.source@1"}}
    for index in range(1, node_count):
        nodes[f"n{index}"] = {
            "block": "custom.step@1",
            "inputs": {"value": f"n{index - 1}.value"},
        }
    return json.dumps(
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "gil-release-probe"},
            "spec": {"nodes": nodes},
        }
    )


def test_native_compile_releases_the_gil() -> None:
    # Find a document whose single compile call is long enough to carry an
    # observable interior window (also serves as the warm-up call).
    node_count = 20_000
    while True:
        document_json = _large_graph_json(node_count)
        probe_started = time.monotonic()
        warmup = json.loads(
            graphblocks_runtime.compile_graph_json(document_json, allow_unknown_blocks=True)
        )
        compile_seconds = time.monotonic() - probe_started
        assert "hash" in warmup
        if compile_seconds >= 0.8 or node_count >= 320_000:
            break
        node_count *= 2

    stamps: list[float] = []
    started = threading.Event()
    stop = threading.Event()

    def spin() -> None:
        started.set()
        last_stamp = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now - last_stamp >= 0.005:
                stamps.append(now)
                last_stamp = now

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=10)
        call_started = time.monotonic()
        graphblocks_runtime.compile_graph_json(document_json, allow_unknown_blocks=True)
        call_ended = time.monotonic()
    finally:
        stop.set()
        thread.join(timeout=10)

    # A GIL-holding binding freezes the sampling thread for the whole call, so
    # its stamps can only cluster at the edges; a margin larger than any
    # scheduler slice keeps those edge stamps out of the interior window.
    margin = 0.15
    assert call_ended - call_started > 2 * margin + 0.1
    interior = [
        stamp for stamp in stamps if call_started + margin < stamp < call_ended - margin
    ]
    assert interior, "compile_graph_json must release the GIL while compiling"
