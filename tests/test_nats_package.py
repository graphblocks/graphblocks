from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_nats(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-nats" / "src"))
    return importlib.import_module("graphblocks_nats")


def test_nats_message_projects_to_durable_source_event(monkeypatch) -> None:
    graphblocks_nats = _import_nats(monkeypatch)
    message = graphblocks_nats.NatsMessage(
        stream="ORDERS",
        subject="orders.created",
        sequence=41,
        payload={"orderId": "ord-41"},
        timestamp_unix_ms=1_820_000_000_041,
        headers={"tenant": "acme"},
    )

    event = message.to_source_event()

    assert event.cursor == graphblocks_nats.SourceCursor("ORDERS", 0, 41)
    assert event.event_time_unix_ms == 1_820_000_000_041
    assert event.payload == {
        "subject": "orders.created",
        "payload": {"orderId": "ord-41"},
        "headers": {"tenant": "acme"},
    }


def test_nats_consumer_cursor_round_trips_durable_cursor(monkeypatch) -> None:
    graphblocks_nats = _import_nats(monkeypatch)
    cursor = graphblocks_nats.SourceCursor("ORDERS", 0, 41)

    nats_cursor = graphblocks_nats.NatsConsumerCursor.from_source_cursor("orders-durable", cursor)

    assert nats_cursor.next_sequence == 42
    assert nats_cursor.to_source_cursor() == cursor
    assert graphblocks_nats.NatsConsumerCursor("orders-durable", "ORDERS", 1).to_source_cursor() is None
    with pytest.raises(graphblocks_nats.NatsAdapterError):
        graphblocks_nats.NatsConsumerCursor.from_source_cursor(
            "orders-durable",
            graphblocks_nats.SourceCursor("ORDERS", 1, 41),
        )
    with pytest.raises(graphblocks_nats.NatsAdapterError):
        graphblocks_nats.NatsConsumerCursor("orders-durable", "ORDERS", 0)


def test_nats_publish_message_projects_durable_sink_commit(monkeypatch) -> None:
    graphblocks_nats = _import_nats(monkeypatch)
    request = graphblocks_nats.SinkCommitRequest(
        run_id="run-1",
        node_id="publish-order",
        node_attempt_id="publish-order-attempt-1",
        idempotency_key="idem-1",
        payload={"orderId": "ord-1"},
        precondition_digest="sha256:precondition",
    )

    publish = graphblocks_nats.NatsPublishMessage.from_sink_commit(
        subject="orders.created",
        request=request,
    )

    assert publish.subject == "orders.created"
    assert publish.payload == {"orderId": "ord-1"}
    assert publish.headers == {
        "Nats-Msg-Id": "idem-1",
        "graphblocks-idempotency-key": "idem-1",
        "graphblocks-node-attempt-id": "publish-order-attempt-1",
        "graphblocks-node-id": "publish-order",
        "graphblocks-precondition-digest": "sha256:precondition",
        "graphblocks-run-id": "run-1",
    }


def test_nats_package_is_cataloged_as_optional_durable_adapter(monkeypatch) -> None:
    _import_nats(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-nats"] == {
        "distribution": "graphblocks-nats",
        "import": "graphblocks_nats",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
