from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_kafka(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-kafka" / "src"))
    return importlib.import_module("graphblocks_kafka")


def test_kafka_record_projects_to_durable_source_event(monkeypatch) -> None:
    graphblocks_kafka = _import_kafka(monkeypatch)
    record = graphblocks_kafka.KafkaRecord(
        topic="orders",
        partition=2,
        offset=41,
        value={"orderId": "ord-41"},
        key="ord-41",
        timestamp_unix_ms=1_820_000_000_041,
        headers={"tenant": "acme"},
    )

    event = record.to_source_event()

    assert event.cursor == graphblocks_kafka.SourceCursor("orders", 2, 41)
    assert event.event_time_unix_ms == 1_820_000_000_041
    assert event.payload == {
        "key": "ord-41",
        "value": {"orderId": "ord-41"},
        "headers": {"tenant": "acme"},
    }


def test_kafka_consumer_cursor_round_trips_durable_cursor(monkeypatch) -> None:
    graphblocks_kafka = _import_kafka(monkeypatch)
    cursor = graphblocks_kafka.SourceCursor("orders", 2, 41)

    kafka_cursor = graphblocks_kafka.KafkaConsumerCursor.from_source_cursor("orders-consumer", cursor)

    assert kafka_cursor.next_offset == 42
    assert kafka_cursor.to_source_cursor() == cursor
    assert graphblocks_kafka.KafkaConsumerCursor("orders-consumer", "orders", 2, 0).to_source_cursor() is None
    with pytest.raises(graphblocks_kafka.KafkaAdapterError):
        graphblocks_kafka.KafkaConsumerCursor("orders-consumer", "orders", 2, -1)


def test_kafka_adapter_rejects_boolean_cursor_numbers(monkeypatch) -> None:
    graphblocks_kafka = _import_kafka(monkeypatch)

    cases = (
        lambda: graphblocks_kafka.KafkaRecord("orders", True, 41, {"orderId": "ord-41"}),  # type: ignore[arg-type]
        lambda: graphblocks_kafka.KafkaRecord("orders", 2, True, {"orderId": "ord-41"}),  # type: ignore[arg-type]
        lambda: graphblocks_kafka.KafkaRecord(
            "orders",
            2,
            41,
            {"orderId": "ord-41"},
            timestamp_unix_ms=True,  # type: ignore[arg-type]
        ),
        lambda: graphblocks_kafka.KafkaConsumerCursor("orders-consumer", "orders", True, 42),  # type: ignore[arg-type]
        lambda: graphblocks_kafka.KafkaConsumerCursor("orders-consumer", "orders", 2, True),  # type: ignore[arg-type]
        lambda: graphblocks_kafka.KafkaSinkRecord("orders-out", {"orderId": "ord-1"}, partition=True),  # type: ignore[arg-type]
    )

    for factory in cases:
        with pytest.raises(graphblocks_kafka.KafkaAdapterError):
            factory()


def test_kafka_sink_record_projects_durable_sink_commit(monkeypatch) -> None:
    graphblocks_kafka = _import_kafka(monkeypatch)
    request = graphblocks_kafka.SinkCommitRequest(
        run_id="run-1",
        node_id="write-order",
        node_attempt_id="write-order-attempt-1",
        idempotency_key="idem-1",
        payload={"orderId": "ord-1"},
        precondition_digest="sha256:precondition",
    )

    record = graphblocks_kafka.KafkaSinkRecord.from_sink_commit(
        topic="orders-out",
        request=request,
        key_field="orderId",
    )

    assert record.topic == "orders-out"
    assert record.key == "ord-1"
    assert record.value == {"orderId": "ord-1"}
    assert record.headers == {
        "graphblocks-idempotency-key": "idem-1",
        "graphblocks-node-attempt-id": "write-order-attempt-1",
        "graphblocks-node-id": "write-order",
        "graphblocks-precondition-digest": "sha256:precondition",
        "graphblocks-run-id": "run-1",
    }


def test_kafka_package_is_cataloged_as_optional_durable_adapter(monkeypatch) -> None:
    _import_kafka(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-kafka"] == {
        "distribution": "graphblocks-kafka",
        "import": "graphblocks_kafka",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
