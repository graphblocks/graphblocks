from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import contextmanager

import graphblocks


@contextmanager
def raises_value_error(pattern: str):
    try:
        yield
    except ValueError as error:
        assert re.search(pattern, str(error)), str(error)
    else:
        raise AssertionError("expected ValueError")


def test_async_operation_result_preserves_committed_effect_after_cancel() -> None:
    result = graphblocks.AsyncOperationResult.cancelled("op-1").with_external_effects(
        [
            graphblocks.ExternalEffectRecord(
                effect_id="effect-ticket-1",
                target="ticket-system",
                operation="ticket.create",
                outcome="committed",
                idempotency_key="idem-ticket-1",
                provider_effect_id="ticket-123",
            )
        ]
    )

    assert result.status == "cancelled"
    assert result.external_effect_was_committed() is True
    assert result.to_json()["external_effects"] == [
        {
            "effect_id": "effect-ticket-1",
            "target": "ticket-system",
            "operation": "ticket.create",
            "outcome": "committed",
            "idempotency_key": "idem-ticket-1",
            "provider_effect_id": "ticket-123",
        }
    ]


def test_async_operation_result_preserves_committed_effect_after_incomplete_late_callback() -> None:
    result = graphblocks.AsyncOperationResult.incomplete("op-1").with_external_effects(
        [
            graphblocks.ExternalEffectRecord(
                effect_id="effect-ci-1",
                target="github-actions",
                operation="workflow_dispatch",
                outcome="committed",
                provider_effect_id="gha-run-1",
            )
        ]
    )

    assert result.status == "incomplete"
    assert result.external_effect_was_committed() is True
    assert result.to_json()["external_effects"][0]["provider_effect_id"] == "gha-run-1"


def test_async_operation_result_rejects_invalid_external_effect_records() -> None:
    with raises_value_error("external effect effect_id must not be empty"):
        graphblocks.ExternalEffectRecord(
            effect_id=" ",
            target="ticket-system",
            operation="ticket.create",
            outcome="committed",
        )

    with raises_value_error("provider identity but no committed external effect"):
        graphblocks.AsyncOperationResult.failed("op-2").with_external_effects(
            [
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-denied",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="no_external_effect",
                    provider_effect_id="ticket-123",
                )
            ]
        )


def test_async_operation_records_callback_wait_metadata_and_state_transitions() -> None:
    operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash="sha256:resume",
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
        expires_at="2026-07-02T00:30:00Z",
    )

    submitted = operation.mark_submitted(
        provider_operation_id="gha-run-1",
        submitted_at="2026-07-02T00:00:01Z",
    )
    waiting = submitted.wait_for_callback()
    received = waiting.mark_callback_received(completed_at="2026-07-02T00:10:00Z")
    resuming = received.mark_resuming()
    completed = resuming.complete(completed_at="2026-07-02T00:10:05Z")

    assert operation.state == graphblocks.AsyncOperationState.CREATED
    assert submitted.state == graphblocks.AsyncOperationState.SUBMITTED
    assert submitted.provider_operation_id == "gha-run-1"
    assert waiting.state == graphblocks.AsyncOperationState.WAITING_CALLBACK
    assert received.state == graphblocks.AsyncOperationState.CALLBACK_RECEIVED
    assert resuming.state == graphblocks.AsyncOperationState.RESUMING
    assert completed.state == graphblocks.AsyncOperationState.COMPLETED
    assert completed.completed_at == "2026-07-02T00:10:05Z"
    assert completed.to_json()["callback_ref"] == "cbep-ci-1"


def test_async_operation_records_polling_metadata_and_terminal_failure() -> None:
    operation = graphblocks.AsyncOperation.created(
        operation_id="op-batch-1",
        run_id="run-1",
        node_id="waitBatch",
        attempt_id="attempt-1",
        kind="external_provider_job",
        expected_schema="schemas/BatchResult@1",
        resume_token_hash="sha256:resume",
        idempotency_key="idem-batch-1",
        created_at="2026-07-02T00:00:00Z",
        polling_ref="poll-batch-1",
        expires_at="2026-07-02T02:00:00Z",
    )

    failed = operation.mark_submitted(submitted_at="2026-07-02T00:00:01Z").start_polling().fail(
        completed_at="2026-07-02T00:45:00Z"
    )

    assert failed.state == graphblocks.AsyncOperationState.FAILED
    assert failed.polling_ref == "poll-batch-1"
    assert failed.completed_at == "2026-07-02T00:45:00Z"


def test_async_operation_rejects_invalid_refs_and_transitions() -> None:
    with raises_value_error("async operation callback_ref is required before waiting_callback"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
        ).wait_for_callback()

    with raises_value_error("async operation polling_ref is required before polling"):
        graphblocks.AsyncOperation.created(
            operation_id="op-batch-1",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-batch-1",
            created_at="2026-07-02T00:00:00Z",
        ).start_polling()

    with raises_value_error("async operation cannot transition from created to completed"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).complete(completed_at="2026-07-02T00:10:05Z")

    with raises_value_error("async operation cannot transition from submitted to callback_received"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").mark_callback_received(
            completed_at="2026-07-02T00:10:00Z"
        )

    completed = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash="sha256:resume",
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received(
        completed_at="2026-07-02T00:10:00Z"
    ).mark_resuming().complete(completed_at="2026-07-02T00:10:05Z")

    with raises_value_error("async operation terminal state cannot transition"):
        completed.mark_resuming()


def test_async_operation_result_exports_are_available() -> None:
    assert "AsyncOperation" in graphblocks.__all__
    assert "AsyncOperationState" in graphblocks.__all__
    assert "AsyncOperationResult" in graphblocks.__all__
    assert "ExternalEffectRecord" in graphblocks.__all__
    assert graphblocks.AsyncOperationState.WAITING_CALLBACK == "waiting_callback"
    assert graphblocks.AsyncOperationResultStatus.CANCELLED == "cancelled"


def run_direct() -> None:
    tests: tuple[Callable[[], None], ...] = (
        test_async_operation_result_preserves_committed_effect_after_cancel,
        test_async_operation_result_preserves_committed_effect_after_incomplete_late_callback,
        test_async_operation_result_rejects_invalid_external_effect_records,
        test_async_operation_records_callback_wait_metadata_and_state_transitions,
        test_async_operation_records_polling_metadata_and_terminal_failure,
        test_async_operation_rejects_invalid_refs_and_transitions,
        test_async_operation_result_exports_are_available,
    )
    for test in tests:
        test()


if __name__ == "__main__":
    run_direct()
