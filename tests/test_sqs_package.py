from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_sqs(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-sqs" / "src"))
    return importlib.import_module("graphblocks_sqs")


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
        "import": "graphblocks_sqs",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
