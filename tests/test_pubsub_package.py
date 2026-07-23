from __future__ import annotations

import importlib

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_pubsub(monkeypatch):
    return importlib.import_module("graphblocks.integrations.pubsub")


def test_pubsub_message_projects_to_durable_source_event(monkeypatch) -> None:
    graphblocks_pubsub = _import_pubsub(monkeypatch)
    message = graphblocks_pubsub.PubsubMessage(
        subscription="orders-sub",
        receive_sequence=41,
        message_id="msg-41",
        ack_id="ack-41",
        data={"orderId": "ord-41"},
        publish_time_unix_ms=1_820_000_000_041,
        attributes={"tenant": "acme"},
        ordering_key="orders",
        delivery_attempt=2,
    )

    event = message.to_source_event()

    assert event.cursor == graphblocks_pubsub.SourceCursor("orders-sub", 0, 41)
    assert event.event_time_unix_ms == 1_820_000_000_041
    assert event.payload == {
        "message_id": "msg-41",
        "ack_id": "ack-41",
        "data": {"orderId": "ord-41"},
        "attributes": {"tenant": "acme"},
        "ordering_key": "orders",
        "delivery_attempt": 2,
    }


def test_pubsub_subscription_cursor_round_trips_durable_cursor(monkeypatch) -> None:
    graphblocks_pubsub = _import_pubsub(monkeypatch)
    cursor = graphblocks_pubsub.SourceCursor("orders-sub", 0, 41)

    pubsub_cursor = graphblocks_pubsub.PubsubSubscriptionCursor.from_source_cursor(cursor)

    assert pubsub_cursor.next_sequence == 42
    assert pubsub_cursor.to_source_cursor() == cursor
    assert graphblocks_pubsub.PubsubSubscriptionCursor("orders-sub", 1).to_source_cursor() is None
    with pytest.raises(graphblocks_pubsub.PubsubAdapterError):
        graphblocks_pubsub.PubsubSubscriptionCursor.from_source_cursor(
            graphblocks_pubsub.SourceCursor("orders-sub", 1, 41)
        )
    with pytest.raises(graphblocks_pubsub.PubsubAdapterError):
        graphblocks_pubsub.PubsubSubscriptionCursor("orders-sub", 0)
    with pytest.raises(graphblocks_pubsub.PubsubAdapterError, match="offset must be positive"):
        graphblocks_pubsub.PubsubSubscriptionCursor.from_source_cursor(
            graphblocks_pubsub.SourceCursor("orders-sub", 0, 0)
        )


def test_pubsub_adapter_rejects_boolean_cursor_numbers(monkeypatch) -> None:
    graphblocks_pubsub = _import_pubsub(monkeypatch)

    cases = (
        lambda: graphblocks_pubsub.PubsubMessage(
            "orders-sub",
            True,  # type: ignore[arg-type]
            "msg-41",
            "ack-41",
            {"orderId": "ord-41"},
        ),
        lambda: graphblocks_pubsub.PubsubMessage(
            "orders-sub",
            41,
            "msg-41",
            "ack-41",
            {"orderId": "ord-41"},
            publish_time_unix_ms=True,  # type: ignore[arg-type]
        ),
        lambda: graphblocks_pubsub.PubsubMessage(
            "orders-sub",
            41,
            "msg-41",
            "ack-41",
            {"orderId": "ord-41"},
            delivery_attempt=True,  # type: ignore[arg-type]
        ),
        lambda: graphblocks_pubsub.PubsubSubscriptionCursor("orders-sub", True),  # type: ignore[arg-type]
    )

    for factory in cases:
        with pytest.raises(graphblocks_pubsub.PubsubAdapterError):
            factory()


def test_pubsub_publish_message_projects_durable_sink_commit(monkeypatch) -> None:
    graphblocks_pubsub = _import_pubsub(monkeypatch)
    request = graphblocks_pubsub.SinkCommitRequest(
        run_id="run-1",
        node_id="publish-order",
        node_attempt_id="publish-order-attempt-1",
        idempotency_key="idem-1",
        payload={"orderId": "ord-1"},
        precondition_digest="sha256:precondition",
    )

    publish = graphblocks_pubsub.PubsubPublishMessage.from_sink_commit(
        topic="orders",
        request=request,
        ordering_key_field="orderId",
    )

    assert publish.topic == "orders"
    assert publish.data == {"orderId": "ord-1"}
    assert publish.ordering_key == "ord-1"
    assert publish.attributes == {
        "graphblocks-idempotency-key": "idem-1",
        "graphblocks-node-attempt-id": "publish-order-attempt-1",
        "graphblocks-node-id": "publish-order",
        "graphblocks-precondition-digest": "sha256:precondition",
        "graphblocks-run-id": "run-1",
    }


def test_pubsub_package_is_cataloged_as_optional_durable_adapter(monkeypatch) -> None:
    _import_pubsub(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-pubsub"] == {
        "distribution": "graphblocks-pubsub",
        "artifact": "graphblocks",
        "component": "graphblocks-pubsub",
        "import": "graphblocks.integrations.pubsub",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
