from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import ContentPart, ToolResult
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


def _checkpoint(graphblocks_durable, checkpoint_id: str, state_revision: int, plan_hash: str):
    return graphblocks_durable.CheckpointBarrier(
        checkpoint_id=checkpoint_id,
        run_id="run-000001",
        release_id="release-2026-06-23",
        deployment_revision_id="deployment-rev-1",
        plan_hash=plan_hash,
        checkpoint_schema=graphblocks_durable.SchemaRef("graphblocks.ai/Checkpoint", 1),
        state_revision=state_revision,
        completed_nodes=("extract",),
        pending_nodes=("load",),
        source_cursors={"orders": graphblocks_durable.SourceCursor("orders", 0, 42)},
        operator_state={"dedupe": {"seen": state_revision}},
        sink_commit_metadata={"warehouse": {"tx": checkpoint_id}},
        schema_versions={"checkpoint": 1},
        created_at_unix_ms=1_820_000_000_000 + state_revision,
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


def test_durable_source_rejects_unknown_cursor_stream(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    source = graphblocks_durable.InMemoryDurableSource("at_least_once", [_order_event(graphblocks_durable, 10)])
    unknown_cursor = graphblocks_durable.SourceCursor("payments", 0, 10)

    with pytest.raises(graphblocks_durable.UnknownSourceCursorError) as commit_error:
        source.commit(unknown_cursor)
    with pytest.raises(graphblocks_durable.UnknownSourceCursorError) as poll_error:
        source.poll(unknown_cursor, demand=1)

    assert commit_error.value.cursor == unknown_cursor
    assert poll_error.value.cursor == unknown_cursor


def test_durable_source_rejects_unknown_cursor_partition(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    source = graphblocks_durable.InMemoryDurableSource("at_least_once", [_order_event(graphblocks_durable, 10)])
    unknown_cursor = graphblocks_durable.SourceCursor("orders", 1, 10)

    with pytest.raises(graphblocks_durable.UnknownSourceCursorError) as commit_error:
        source.commit(unknown_cursor)
    with pytest.raises(graphblocks_durable.UnknownSourceCursorError) as poll_error:
        source.poll(unknown_cursor, demand=1)

    assert commit_error.value.cursor == unknown_cursor
    assert poll_error.value.cursor == unknown_cursor


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("", 0, 10), "stream must not be empty"),
        (("orders", True, 10), "partition must be an integer"),
        (("orders", 0, False), "offset must be an integer"),
        (("orders", -1, 10), "partition must be non-negative"),
        (("orders", 0, -1), "offset must be non-negative"),
    ],
)
def test_durable_source_cursor_validates_identity_fields(
    monkeypatch,
    args: tuple[object, object, object],
    message: str,
) -> None:
    graphblocks_durable = _import_durable(monkeypatch)

    with pytest.raises(graphblocks_durable.DurableError, match=message):
        graphblocks_durable.SourceCursor(*args)


def test_durable_source_rejects_invalid_delivery_guarantee(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)

    with pytest.raises(graphblocks_durable.DurableError, match="unsupported delivery guarantee"):
        graphblocks_durable.InMemoryDurableSource("exactly_once", [])


@pytest.mark.parametrize(
    ("demand", "message"),
    [
        (True, "demand must be an integer"),
        (0, "demand must be positive"),
    ],
)
def test_durable_source_batch_validates_guarantee_and_demand(
    monkeypatch,
    demand: object,
    message: str,
) -> None:
    graphblocks_durable = _import_durable(monkeypatch)

    with pytest.raises(graphblocks_durable.DurableError, match="unsupported delivery guarantee"):
        graphblocks_durable.SourceBatch.new("exactly_once", [], None, demand=1)
    with pytest.raises(graphblocks_durable.DurableError, match=message):
        graphblocks_durable.SourceBatch.new("at_least_once", [], None, demand=demand)


@pytest.mark.parametrize(
    ("event_time_unix_ms", "message"),
    [
        (False, "event_time_unix_ms must be an integer"),
        (-1, "event_time_unix_ms must be non-negative"),
    ],
)
def test_durable_source_event_validates_event_time(
    monkeypatch,
    event_time_unix_ms: object,
    message: str,
) -> None:
    graphblocks_durable = _import_durable(monkeypatch)

    with pytest.raises(graphblocks_durable.DurableError, match=message):
        graphblocks_durable.SourceEvent(
            graphblocks_durable.SourceCursor("orders", 0, 10),
            {"orderId": "ord-10"},
            event_time_unix_ms=event_time_unix_ms,
        )


@pytest.mark.parametrize(
    ("unix_ms", "message"),
    [
        (True, "watermark unix_ms must be an integer"),
        (-1, "watermark unix_ms must be non-negative"),
    ],
)
def test_durable_watermark_validates_unix_ms(monkeypatch, unix_ms: object, message: str) -> None:
    graphblocks_durable = _import_durable(monkeypatch)

    with pytest.raises(graphblocks_durable.DurableError, match=message):
        graphblocks_durable.Watermark.event_time(unix_ms)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"size_ms": True}, "window size_ms must be an integer"),
        ({"allowed_lateness_ms": False}, "allowed_lateness_ms must be an integer"),
        ({"size_ms": 0}, "window size_ms must be positive"),
        ({"allowed_lateness_ms": -1}, "allowed_lateness_ms must be non-negative"),
    ],
)
def test_durable_window_policy_validates_integer_bounds(
    monkeypatch,
    kwargs: dict[str, object],
    message: str,
) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    config = {
        "size_ms": 1_000,
        "allowed_lateness_ms": 250,
        "accumulation_mode": "discarding",
        **kwargs,
    }

    with pytest.raises(graphblocks_durable.DurableError, match=message):
        graphblocks_durable.WindowPolicy.tumbling_event_time(**config)


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


def test_durable_tool_terminal_store_replays_incomplete_terminal_record(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    record = graphblocks_durable.DurableToolTerminalRecord(
        run_id="run-000001",
        response_id="response-1",
        tool_call_id="call-1",
        revision=1,
        terminal_state="incomplete",
        arguments_digest="sha256:arguments",
        completed_at_unix_ms=1_820_000_000_000,
    )

    committed = store.record_tool_terminal(record)
    replayed = store.record_tool_terminal(record)

    assert committed.sequence == 1
    assert committed.record.terminal_state == "incomplete"
    assert committed.record.output_digest is None
    assert committed.record.effect_committed is False
    assert committed.record.durable_result_committed is False
    assert replayed.sequence == committed.sequence
    assert replayed.record == committed.record
    assert replayed.replayed is True
    assert store.tool_terminal_count() == 1


def test_durable_tool_terminal_record_projects_completed_tool_result(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    result = ToolResult.completed(
        "call-2",
        (ContentPart(kind="text", text="created"),),
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    ).with_effect_outcome("committed")

    record = graphblocks_durable.DurableToolTerminalRecord.from_tool_result(
        result,
        run_id="run-000001",
        response_id="response-1",
        revision=1,
        arguments_digest="sha256:arguments-2",
        completed_at_unix_ms=1_820_000_000_100,
        idempotency_key="ticket-create:call-2",
        durable_result_committed=True,
    )

    assert record.tool_call_id == "call-2"
    assert record.terminal_state == "completed"
    assert record.output_digest == result.output_digest
    assert record.idempotency_key == "ticket-create:call-2"
    assert record.effect_committed is True
    assert record.durable_result_committed is True

    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    committed = store.record_tool_terminal(record)
    assert committed.sequence == 1
    assert committed.record == record


def test_durable_tool_terminal_record_projects_policy_stopped_effect(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    result = ToolResult.policy_stopped(
        "call-3",
        error={"code": "policy.denied", "message": "tool output was stopped after commit"},
        started_at="2026-06-23T00:00:00Z",
        completed_at="2026-06-23T00:00:01Z",
    ).with_effect_outcome("committed")

    record = graphblocks_durable.DurableToolTerminalRecord.from_tool_result(
        result,
        run_id="run-000001",
        response_id="response-2",
        revision=1,
        arguments_digest="sha256:arguments-3",
        completed_at_unix_ms=1_820_000_000_200,
    )

    assert record.terminal_state == "policy_stopped"
    assert record.output_digest is None
    assert record.effect_committed is True
    assert record.durable_result_committed is False

    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    store.record_response_policy_stopped(
        "response-2",
        "decision-abort",
        last_policy_accepted_sequence=7,
        occurred_at_unix_ms=1_820_000_000_300,
    )
    committed = store.record_tool_terminal(record)
    assert committed.record.terminal_state == "policy_stopped"
    assert committed.record.effect_committed is True
    assert committed.record.durable_result_committed is False


def test_durable_tool_terminal_store_rejects_terminal_replay_mutation(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    record = graphblocks_durable.DurableToolTerminalRecord(
        run_id="run-000001",
        response_id="response-1",
        tool_call_id="call-1",
        revision=1,
        terminal_state="completed",
        arguments_digest="sha256:arguments",
        completed_at_unix_ms=1_820_000_000_000,
        output_digest="sha256:output",
        idempotency_key="ticket-create:call-1",
        effect_committed=True,
        durable_result_committed=True,
    )
    conflicting = graphblocks_durable.DurableToolTerminalRecord(
        run_id=record.run_id,
        response_id=record.response_id,
        tool_call_id=record.tool_call_id,
        revision=record.revision,
        terminal_state="failed",
        arguments_digest=record.arguments_digest,
        completed_at_unix_ms=record.completed_at_unix_ms,
        output_digest="sha256:error",
    )

    store.record_tool_terminal(record)

    with pytest.raises(graphblocks_durable.ToolTerminalStateConflictError) as error:
        store.record_tool_terminal(conflicting)

    assert error.value.response_id == "response-1"
    assert error.value.tool_call_id == "call-1"
    assert error.value.revision == 1
    assert store.tool_terminal_count() == 1


def test_durable_tool_terminal_store_replays_response_policy_stop_barrier(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()

    committed = store.record_response_policy_stopped(
        "response-1",
        "decision-abort",
        last_policy_accepted_sequence=7,
        occurred_at_unix_ms=1_820_000_000_000,
    )
    replayed = store.record_response_policy_stopped(
        "response-1",
        "decision-abort",
        last_policy_accepted_sequence=7,
        occurred_at_unix_ms=1_820_000_000_000,
    )

    assert committed.sequence == 1
    assert committed.record.response_id == "response-1"
    assert committed.record.stream_id == "response-1"
    assert committed.record.policy_decision_id == "decision-abort"
    assert committed.record.last_generated_sequence == 7
    assert committed.record.last_client_delivered_sequence == 7
    assert replayed.sequence == committed.sequence
    assert replayed.record == committed.record
    assert replayed.replayed is True


def test_durable_tool_terminal_store_persists_full_output_cutoff_state(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()

    committed = store.record_response_policy_stopped(
        "response-1",
        "decision-abort",
        stream_id="stream-1",
        turn_id="turn-1",
        last_generated_sequence=9,
        last_policy_accepted_sequence=7,
        last_client_delivered_sequence=6,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
        occurred_at_unix_ms=1_820_000_000_000,
    )
    replayed = store.record_response_policy_stopped(
        "response-1",
        "decision-abort",
        stream_id="stream-1",
        turn_id="turn-1",
        last_generated_sequence=9,
        last_policy_accepted_sequence=7,
        last_client_delivered_sequence=6,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
        occurred_at_unix_ms=1_820_000_000_000,
    )

    assert committed.record.stream_id == "stream-1"
    assert committed.record.turn_id == "turn-1"
    assert committed.record.last_generated_sequence == 9
    assert committed.record.last_policy_accepted_sequence == 7
    assert committed.record.last_client_delivered_sequence == 6
    assert committed.record.terminal_reason == "policy_denied"
    assert committed.record.draft_disposition == "retract"
    assert committed.record.durable_result == "none"
    assert replayed.record == committed.record
    assert replayed.replayed is True


def test_durable_response_policy_stop_record_converts_to_output_cutoff(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    record = graphblocks_durable.DurableResponsePolicyStopRecord(
        response_id="response-1",
        policy_decision_id="decision-abort",
        last_policy_accepted_sequence=7,
        occurred_at_unix_ms=1_820_000_000_000,
        stream_id="stream-1",
        turn_id="turn-1",
        last_generated_sequence=9,
        last_client_delivered_sequence=6,
        terminal_reason="policy_denied",
        draft_disposition="retract",
        durable_result="none",
    )

    cutoff = record.to_output_cutoff(occurred_at="2026-06-23T00:00:02Z")

    assert cutoff.stream_id == "stream-1"
    assert cutoff.response_id == "response-1"
    assert cutoff.turn_id == "turn-1"
    assert cutoff.last_generated_sequence == 9
    assert cutoff.last_policy_accepted_sequence == 7
    assert cutoff.last_client_delivered_sequence == 6
    assert cutoff.terminal_reason == "policy_denied"
    assert cutoff.draft_disposition == "retract"
    assert cutoff.durable_result == "none"
    assert cutoff.policy_decision_id == "decision-abort"
    assert cutoff.occurred_at == "2026-06-23T00:00:02Z"


def test_durable_tool_terminal_store_rejects_late_result_commit_after_policy_stop(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    store.record_response_policy_stopped(
        "response-1",
        "decision-abort",
        last_policy_accepted_sequence=7,
        occurred_at_unix_ms=1_820_000_000_000,
    )
    durable_result = graphblocks_durable.DurableToolTerminalRecord(
        run_id="run-000001",
        response_id="response-1",
        tool_call_id="call-1",
        revision=1,
        terminal_state="completed",
        arguments_digest="sha256:arguments",
        completed_at_unix_ms=1_820_000_000_100,
        output_digest="sha256:output",
        durable_result_committed=True,
    )
    audited_late_effect = graphblocks_durable.DurableToolTerminalRecord(
        run_id="run-000001",
        response_id="response-1",
        tool_call_id="call-2",
        revision=1,
        terminal_state="cancelled",
        arguments_digest="sha256:arguments-late",
        completed_at_unix_ms=1_820_000_000_200,
        effect_committed=True,
    )

    with pytest.raises(graphblocks_durable.ResponsePolicyStoppedError) as error:
        store.record_tool_terminal(durable_result)

    committed = store.record_tool_terminal(audited_late_effect)

    assert error.value.response_id == "response-1"
    assert committed.record.terminal_state == "cancelled"
    assert committed.record.effect_committed is True
    assert committed.record.durable_result_committed is False
    assert store.tool_terminal_count() == 1


def test_durable_tool_terminal_store_rejects_policy_stop_after_committed_result(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryDurableToolTerminalStore()
    store.record_tool_terminal(
        graphblocks_durable.DurableToolTerminalRecord(
            run_id="run-000001",
            response_id="response-1",
            tool_call_id="call-1",
            revision=1,
            terminal_state="completed",
            arguments_digest="sha256:arguments",
            completed_at_unix_ms=1_820_000_000_000,
            output_digest="sha256:output",
            durable_result_committed=True,
        )
    )

    with pytest.raises(graphblocks_durable.DurableResultAlreadyCommittedError) as error:
        store.record_response_policy_stopped(
            "response-1",
            "decision-abort",
            last_policy_accepted_sequence=7,
            occurred_at_unix_ms=1_820_000_000_100,
        )

    assert error.value.response_id == "response-1"


def test_durable_checkpoint_barrier_validates_and_builds_source_commit_plan(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    missing_plan = _checkpoint(graphblocks_durable, "checkpoint-000001", 1, "")

    with pytest.raises(graphblocks_durable.CheckpointBarrierError) as plan_error:
        missing_plan.validate()

    assert plan_error.value.reason == "missing_plan_hash"

    invalid_schema = graphblocks_durable.CheckpointBarrier(
        checkpoint_id="checkpoint-000001",
        run_id="run-000001",
        release_id="release-2026-06-23",
        deployment_revision_id="deployment-rev-1",
        plan_hash="sha256:plan",
        checkpoint_schema=graphblocks_durable.SchemaRef("", 1),
        state_revision=1,
        completed_nodes=(),
        pending_nodes=(),
        source_cursors={},
        operator_state={},
        sink_commit_metadata={},
        schema_versions={"checkpoint": 1},
        created_at_unix_ms=1_820_000_000_001,
    )
    with pytest.raises(graphblocks_durable.CheckpointBarrierError) as schema_error:
        invalid_schema.validate()

    assert schema_error.value.reason == "invalid_checkpoint_schema"

    barrier = _checkpoint(graphblocks_durable, "checkpoint-000002", 2, "sha256:plan").with_source_cursor(
        "payments",
        graphblocks_durable.SourceCursor("payments", 1, 7),
    )

    plan = barrier.validate().source_commit_plan()

    assert plan.cursors == (
        ("orders", graphblocks_durable.SourceCursor("orders", 0, 42)),
        ("payments", graphblocks_durable.SourceCursor("payments", 1, 7)),
    )


def test_durable_checkpoint_store_replays_latest_compatible_checkpoint(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryCheckpointStore()
    store.put(_checkpoint(graphblocks_durable, "checkpoint-000001", 1, "sha256:plan"))
    store.put(_checkpoint(graphblocks_durable, "checkpoint-000002", 2, "sha256:plan"))
    store.put(_checkpoint(graphblocks_durable, "checkpoint-000003", 3, "sha256:other-plan"))

    replay = store.latest_compatible(
        run_id="run-000001",
        release_id="release-2026-06-23",
        deployment_revision_id="deployment-rev-1",
        plan_hash="sha256:plan",
    )

    assert replay is not None
    assert replay.checkpoint_id == "checkpoint-000002"
    assert replay.state_revision == 2
    assert (
        store.latest_compatible(
            run_id="run-000001",
            release_id="release-2026-06-23",
            deployment_revision_id="deployment-rev-2",
            plan_hash="sha256:plan",
        )
        is None
    )


def test_durable_checkpoint_store_rejects_stale_state_revision(monkeypatch) -> None:
    graphblocks_durable = _import_durable(monkeypatch)
    store = graphblocks_durable.InMemoryCheckpointStore()
    store.put(_checkpoint(graphblocks_durable, "checkpoint-000002", 2, "sha256:plan"))

    with pytest.raises(graphblocks_durable.StaleCheckpointError) as error:
        store.put(_checkpoint(graphblocks_durable, "checkpoint-000001", 1, "sha256:plan"))

    assert error.value.run_id == "run-000001"
    assert error.value.current == 2
    assert error.value.attempted == 1


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
