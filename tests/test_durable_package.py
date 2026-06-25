from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_durable(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    return importlib.import_module("graphblocks_durable")


def _order_event(graphblocks_durable, offset: int):
    return graphblocks_durable.SourceEvent(
        graphblocks_durable.SourceCursor("orders", 0, offset),
        {"orderId": f"ord-{offset}"},
        event_time_unix_ms=1_820_000_000_000 + offset,
    )


def test_durable_source_replays_from_committed_or_explicit_cursor(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    source = graphblocks_durable.InMemoryDurableSource(
        "at_least_once",
        [_order_event(graphblocks_durable, 10), _order_event(graphblocks_durable, 11), _order_event(graphblocks_durable, 12)],
    )

    first = source.poll(None, demand=2)
    source.commit(graphblocks_durable.SourceCursor("orders", 0, 11))
    after_commit = source.poll(None, demand=2)
    replay = source.poll(graphblocks_durable.SourceCursor("orders", 0, 10), demand=2)

    assert [event.cursor.offset for event in first.events] == [10, 11]
    assert [event.cursor.offset for event in after_commit.events] == [12]
    assert [event.cursor.offset for event in replay.events] == [11, 12]
    assert first.high_cursor() == graphblocks_durable.SourceCursor("orders", 0, 11)
    assert first.watermark == graphblocks_durable.Watermark.event_time(1_820_000_000_011)


def test_durable_source_pause_and_stale_commit(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    source = graphblocks_durable.InMemoryDurableSource("at_least_once", [_order_event(graphblocks_durable, 10)])

    source.pause()
    with pytest.raises(graphblocks_durable.SourcePausedError):
        source.poll(None, demand=1)
    source.resume()
    assert len(source.poll(None, demand=1).events) == 1

    source.commit(graphblocks_durable.SourceCursor("orders", 0, 10))
    with pytest.raises(graphblocks_durable.StaleCommitError) as error:
        source.commit(graphblocks_durable.SourceCursor("orders", 0, 9))

    assert error.value.current == graphblocks_durable.SourceCursor("orders", 0, 10)
    assert error.value.attempted == graphblocks_durable.SourceCursor("orders", 0, 9)


def test_durable_event_time_window_closes_after_watermark_and_rejects_late_events(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    policy = graphblocks_durable.WindowPolicy.tumbling_event_time(
        size_ms=1_000,
        allowed_lateness_ms=250,
        accumulation_mode="discarding",
    )
    windows = graphblocks_durable.WindowAccumulator(policy)

    windows.ingest(_order_event(graphblocks_durable, 100))
    windows.ingest(_order_event(graphblocks_durable, 900))
    assert windows.advance_watermark(graphblocks_durable.Watermark.event_time(1_820_000_001_249)) == []
    closed = windows.advance_watermark(graphblocks_durable.Watermark.event_time(1_820_000_001_250))

    assert len(closed) == 1
    assert closed[0].start_unix_ms == 1_820_000_000_000
    assert closed[0].end_unix_ms == 1_820_000_001_000
    assert [event.cursor.offset for event in closed[0].events] == [100, 900]
    with pytest.raises(graphblocks_durable.LateEventError) as error:
        windows.ingest(_order_event(graphblocks_durable, 999))
    assert error.value.watermark_unix_ms == 1_820_000_001_250


def test_durable_sink_commit_replays_same_idempotency_key_and_rejects_conflict(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    sink = graphblocks_durable.InMemoryDurableSink("orders-sink")
    request = graphblocks_durable.SinkCommitRequest(
        run_id="run-1",
        node_id="write-order",
        node_attempt_id="write-order-attempt-1",
        idempotency_key="idem-1",
        payload={"orderId": "ord-1"},
    ).with_precondition_digest("sha256:precondition")

    first = sink.commit(request)
    replay = sink.commit(request)

    assert first.sequence == 1
    assert first.replayed is False
    assert replay.sequence == 1
    assert replay.replayed is True
    assert sink.committed_count() == 1
    with pytest.raises(graphblocks_durable.IdempotencyConflictError):
        sink.commit(
            graphblocks_durable.SinkCommitRequest(
                run_id=request.run_id,
                node_id=request.node_id,
                node_attempt_id=request.node_attempt_id,
                idempotency_key=request.idempotency_key,
                payload={"orderId": "ord-2"},
                precondition_digest=request.precondition_digest,
            )
        )


def test_durable_package_is_cataloged_as_optional_extension(monkeypatch) -> None:
    _import_durable(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-durable"] == {
        "distribution": "graphblocks-durable",
        "import": "graphblocks_durable",
        "default": False,
        "layer": "durable_stream",
        "kind": "pure_python",
        "implementationPhase": 7,
        "stability": "experimental-extension",
    }
