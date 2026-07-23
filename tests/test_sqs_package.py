from __future__ import annotations

import importlib
import json

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_sqs(monkeypatch):
    return importlib.import_module("graphblocks.integrations.sqs")


def test_sqs_message_projects_to_durable_source_event(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)
    message = graphblocks_sqs.SqsMessage(
        queue="orders",
        receive_sequence=41,
        message_id="msg-41",
        receipt_handle="receipt-41",
        body={"orderId": "ord-41"},
        sent_timestamp_unix_ms=1_820_000_000_041,
        attributes={"ApproximateReceiveCount": "1"},
        message_attributes={"tenant": "acme"},
    )

    event = message.to_source_event()

    assert event.cursor == graphblocks_sqs.SourceCursor("orders", 0, 41)
    assert event.event_time_unix_ms == 1_820_000_000_041
    assert event.payload == {
        "message_id": "msg-41",
        "receipt_handle": "receipt-41",
        "body": {"orderId": "ord-41"},
        "attributes": {"ApproximateReceiveCount": "1"},
        "message_attributes": {"tenant": "acme"},
    }


def test_sqs_receive_cursor_round_trips_durable_cursor(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)
    cursor = graphblocks_sqs.SourceCursor("orders", 0, 41)

    sqs_cursor = graphblocks_sqs.SqsReceiveCursor.from_source_cursor(cursor)

    assert sqs_cursor.next_sequence == 42
    assert sqs_cursor.to_source_cursor() == cursor
    assert graphblocks_sqs.SqsReceiveCursor("orders", 1).to_source_cursor() is None
    with pytest.raises(graphblocks_sqs.SqsAdapterError):
        graphblocks_sqs.SqsReceiveCursor.from_source_cursor(graphblocks_sqs.SourceCursor("orders", 1, 41))
    with pytest.raises(graphblocks_sqs.SqsAdapterError):
        graphblocks_sqs.SqsReceiveCursor("orders", 0)
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="offset must be positive"):
        graphblocks_sqs.SqsReceiveCursor.from_source_cursor(
            graphblocks_sqs.SourceCursor("orders", 0, 0)
        )


def test_sqs_adapter_rejects_boolean_cursor_numbers(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)

    cases = (
        lambda: graphblocks_sqs.SqsMessage(
            "orders",
            True,  # type: ignore[arg-type]
            "msg-41",
            "receipt-41",
            {"orderId": "ord-41"},
        ),
        lambda: graphblocks_sqs.SqsMessage(
            "orders",
            41,
            "msg-41",
            "receipt-41",
            {"orderId": "ord-41"},
            sent_timestamp_unix_ms=True,  # type: ignore[arg-type]
        ),
        lambda: graphblocks_sqs.SqsReceiveCursor("orders", True),  # type: ignore[arg-type]
    )

    for factory in cases:
        with pytest.raises(graphblocks_sqs.SqsAdapterError):
            factory()


def test_sqs_adapter_rejects_malformed_strings_and_attributes(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)

    for kwargs in (
        {"queue": object()},
        {"message_id": " msg-41"},
        {"receipt_handle": object()},
        {"attributes": {"ApproximateReceiveCount": 1}},
        {"message_attributes": {7: "tenant"}},
    ):
        with pytest.raises(graphblocks_sqs.SqsAdapterError):
            graphblocks_sqs.SqsMessage(
                **{
                    "queue": "orders",
                    "receive_sequence": 41,
                    "message_id": "msg-41",
                    "receipt_handle": "receipt-41",
                    "body": {},
                    **kwargs,
                }
            )


def test_sqs_rejects_overflowing_cursors_and_timestamps(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)

    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="unsigned 64-bit"):
        graphblocks_sqs.SqsMessage(
            "orders",
            1 << 64,
            "msg-1",
            "receipt-1",
            {},
        )
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="signed 64-bit"):
        graphblocks_sqs.SqsMessage(
            "orders",
            1,
            "msg-1",
            "receipt-1",
            {},
            sent_timestamp_unix_ms=1 << 63,
        )
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="cannot advance"):
        graphblocks_sqs.SqsReceiveCursor.from_source_cursor(
            graphblocks_sqs.SourceCursor("orders", 0, (1 << 64) - 1)
        )


def test_sqs_rejects_unstable_attributes_and_incomplete_fifo_identity(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)

    class BrokenAttributes(dict[str, str]):
        def items(self):
            raise RuntimeError("mapping changed during iteration")

    for attributes in (
        BrokenAttributes(),
        {"trace": "\ud800"},
        {"bad attribute": "value"},
        {"AWS.reserved": "value"},
    ):
        with pytest.raises(graphblocks_sqs.SqsAdapterError, match="attributes"):
            graphblocks_sqs.SqsMessage(
                "orders",
                1,
                "msg-1",
                "receipt-1",
                {},
                attributes=attributes,
            )
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="message_group_id"):
        graphblocks_sqs.SqsSendMessage(
            "orders.fifo",
            {},
            message_deduplication_id="idem-1",
        )


def test_sqs_message_snapshots_body_and_rejects_non_boolean_fifo(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)
    body = {"order": {"state": "created"}}
    message = graphblocks_sqs.SqsMessage(
        "orders", 1, "msg-1", "receipt-1", body
    )
    body["order"]["state"] = "cancelled"

    assert message.to_source_event().payload["body"] == {"order": {"state": "created"}}

    request = graphblocks_sqs.SinkCommitRequest(
        "run-1", "node-1", "attempt-1", "idem-1", {}
    )
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="fifo"):
        graphblocks_sqs.SqsSendMessage.from_sink_commit(
            queue="orders", request=request, fifo="false"  # type: ignore[arg-type]
        )


def test_sqs_messages_expose_immutable_strict_json_snapshots(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)
    message = graphblocks_sqs.SqsMessage(
        queue="orders",
        receive_sequence=1,
        message_id="msg-1",
        receipt_handle="receipt-1",
        body={"items": [{"state": "created"}]},
        attributes={"tenant": "acme"},
    )

    with pytest.raises(TypeError):
        message.body["items"][0]["state"] = "cancelled"
    with pytest.raises(TypeError):
        dict.__setitem__(message.body, "items", [])
    with pytest.raises(TypeError):
        message.attributes["tenant"] = "other"
    projected = message.to_source_event()
    json.dumps(projected.payload)
    projected.payload["body"]["items"][0]["state"] = "mutated"

    assert message.to_source_event().payload["body"] == {
        "items": [{"state": "created"}]
    }
    with pytest.raises(graphblocks_sqs.SqsAdapterError, match="strict JSON"):
        graphblocks_sqs.SqsSendMessage(
            "orders",
            {"items": ("not", "json")},
        )


def test_sqs_send_message_projects_durable_sink_commit(monkeypatch) -> None:
    graphblocks_sqs = _import_sqs(monkeypatch)
    request = graphblocks_sqs.SinkCommitRequest(
        run_id="run-1",
        node_id="send-order",
        node_attempt_id="send-order-attempt-1",
        idempotency_key="idem-1",
        payload={"orderId": "ord-1"},
        precondition_digest="sha256:precondition",
    )

    send = graphblocks_sqs.SqsSendMessage.from_sink_commit(
        queue="orders.fifo",
        request=request,
        fifo=True,
        message_group_id="orders",
    )

    assert send.queue == "orders.fifo"
    assert send.body == {"orderId": "ord-1"}
    assert send.message_group_id == "orders"
    assert send.message_deduplication_id == "idem-1"
    assert send.message_attributes == {
        "graphblocks-idempotency-key": "idem-1",
        "graphblocks-node-attempt-id": "send-order-attempt-1",
        "graphblocks-node-id": "send-order",
        "graphblocks-precondition-digest": "sha256:precondition",
        "graphblocks-run-id": "run-1",
    }


def test_sqs_package_is_cataloged_as_optional_durable_adapter(monkeypatch) -> None:
    _import_sqs(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-sqs"] == {
        "distribution": "graphblocks-sqs",
        "artifact": "graphblocks",
        "component": "graphblocks-sqs",
        "import": "graphblocks.integrations.sqs",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
