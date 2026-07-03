from __future__ import annotations

import re
import math
import random
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

    with raises_value_error("async operation result external_effects must be a sequence"):
        graphblocks.AsyncOperationResult.cancelled("op-3").with_external_effects("effect-ticket-1")  # type: ignore[arg-type]

    with raises_value_error("async operation result external_effects must be a sequence"):
        graphblocks.AsyncOperationResult.cancelled("op-4").with_external_effects(object())  # type: ignore[arg-type]

    with raises_value_error("async operation result external_effects must be a sequence"):
        graphblocks.AsyncOperationResult(
            operation_id="op-5",
            status="cancelled",
            external_effects=object(),  # type: ignore[arg-type]
        )


def test_async_operation_result_rejects_duplicate_external_effect_ids() -> None:
    with raises_value_error("async operation result external_effects must not contain duplicate effect_id"):
        graphblocks.AsyncOperationResult.cancelled("op-1").with_external_effects(
            [
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-ticket-1",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="committed",
                    provider_effect_id="ticket-123",
                ),
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-ticket-1",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="committed",
                    provider_effect_id="ticket-123",
                ),
            ]
        )


def test_async_operation_result_rejects_duplicate_provider_effect_ids() -> None:
    with raises_value_error("async operation result external_effects must not contain duplicate provider_effect_id"):
        graphblocks.AsyncOperationResult.cancelled("op-1").with_external_effects(
            [
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-ticket-1",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="committed",
                    provider_effect_id="ticket-123",
                ),
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-ticket-2",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="committed",
                    provider_effect_id="ticket-123",
                ),
            ]
        )


def test_async_operation_result_deep_copies_json_output_and_projection_sequences() -> None:
    output = {"summary": {"passed": True, "checks": ["lint"]}}
    artifacts = [{"artifact_id": "artifact-1", "uri": "blob://ci/log"}]
    diagnostics = [{"code": "ci.warning", "message": "slow test"}]
    metrics = [{"name": "duration_ms", "value": 128}]
    checks = [{"name": "unit", "status": "passed"}]
    usage = [{"kind": "ci_minutes", "amount": 2}]

    result = graphblocks.AsyncOperationResult.completed(
        "op-1",
        output=output,
    ).with_projections(
        artifacts=artifacts,
        diagnostics=diagnostics,
        metrics=metrics,
        checks=checks,
        usage=usage,
    )

    output["summary"]["checks"].append("mutated")  # type: ignore[index, union-attr]
    artifacts[0]["uri"] = "blob://ci/mutated"
    projected = result.to_json()
    projected["output"]["summary"]["checks"].append("caller-mutation")  # type: ignore[index, union-attr]
    projected["artifacts"][0]["uri"] = "blob://ci/caller-mutation"  # type: ignore[index]

    assert result.output == {"summary": {"passed": True, "checks": ("lint",)}}
    assert result.artifacts == ({"artifact_id": "artifact-1", "uri": "blob://ci/log"},)
    assert result.to_json()["output"] == {"summary": {"passed": True, "checks": ["lint"]}}
    assert result.to_json()["artifacts"] == [{"artifact_id": "artifact-1", "uri": "blob://ci/log"}]


def test_async_operation_result_rejects_non_json_output_and_projection_values() -> None:
    with raises_value_error("async operation result output must contain only JSON values"):
        graphblocks.AsyncOperationResult.completed("op-1", output=object())

    with raises_value_error("async operation result output must not contain non-finite numbers"):
        graphblocks.AsyncOperationResult.completed("op-1", output={"value": math.nan})

    with raises_value_error("async operation result artifacts must contain only JSON values"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(artifacts=[{"bad": object()}])

    with raises_value_error("async operation result metrics must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(metrics=object())  # type: ignore[arg-type]

    with raises_value_error("async operation result checks must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(checks="unit")  # type: ignore[arg-type]


def test_async_operation_result_projects_from_terminal_operation_state() -> None:
    completed_operation = graphblocks.AsyncOperation.created(
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
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received(
        completed_at="2026-07-02T00:10:00Z"
    ).mark_resuming().complete(completed_at="2026-07-02T00:10:05Z")
    cancelled_operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-2",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash="sha256:resume",
        idempotency_key="idem-ci-2",
        created_at="2026-07-02T00:00:00Z",
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").cancel(completed_at="2026-07-02T00:01:00Z")

    completed_result = graphblocks.AsyncOperationResult.from_operation(
        completed_operation,
        output={"summary": "ok"},
    )
    cancelled_result = graphblocks.AsyncOperationResult.from_operation(cancelled_operation)

    assert completed_result.operation_id == "op-ci-1"
    assert completed_result.status == graphblocks.AsyncOperationResultStatus.COMPLETED
    assert completed_result.output == {"summary": "ok"}
    assert cancelled_result.operation_id == "op-ci-2"
    assert cancelled_result.status == graphblocks.AsyncOperationResultStatus.CANCELLED


def test_async_operation_result_rejects_projection_from_non_terminal_operation() -> None:
    waiting = graphblocks.AsyncOperation.created(
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
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback()

    with raises_value_error("async operation result requires a terminal operation"):
        graphblocks.AsyncOperationResult.from_operation(waiting)


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
        expires_at="2026-07-02T00:30:00Z",
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received(
        completed_at="2026-07-02T00:10:00Z"
    ).mark_resuming().complete(completed_at="2026-07-02T00:10:05Z")

    with raises_value_error("async operation terminal state cannot transition"):
        completed.mark_resuming()


def test_async_operation_rejects_state_timestamp_inconsistency() -> None:
    with raises_value_error("async operation submitted state requires submitted_at"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="submitted",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
        )

    with raises_value_error("async operation terminal state requires completed_at"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="completed",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
        )

    with raises_value_error("async operation created state must not have submitted_at or completed_at"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="created",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            completed_at="2026-07-02T00:10:05Z",
        )


def test_async_operation_rejects_direct_wait_states_without_required_refs() -> None:
    with raises_value_error("async operation waiting_callback state requires callback_ref"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="waiting_callback",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        )

    with raises_value_error("async operation callback_received state requires callback_ref"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="callback_received",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
            completed_at="2026-07-02T00:10:00Z",
            expires_at="2026-07-02T00:30:00Z",
        )

    with raises_value_error("async operation polling state requires polling_ref"):
        graphblocks.AsyncOperation(
            operation_id="op-batch-1",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            state="polling",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-batch-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T02:00:00Z",
        )


def test_async_operation_rejects_direct_unbounded_wait_states() -> None:
    with raises_value_error("async operation waiting_callback state requires expires_at or explicit infinite_wait_policy"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="waiting_callback",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            callback_ref="cbep-ci-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
        )

    with raises_value_error("async operation polling state requires expires_at or explicit infinite_wait_policy"):
        graphblocks.AsyncOperation(
            operation_id="op-batch-1",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            state="polling",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-batch-1",
            polling_ref="poll-batch-1",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
        )


def test_async_operation_rejects_provider_identity_before_submission() -> None:
    with raises_value_error("async operation provider_operation_id requires submitted_at"):
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
            provider_operation_id="gha-run-1",
        )


def test_async_operation_rejects_unbounded_callback_and_polling_waits() -> None:
    with raises_value_error("async operation callback wait requires expires_at or explicit infinite_wait_policy"):
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
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback()

    with raises_value_error("async operation polling wait requires expires_at or explicit infinite_wait_policy"):
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
            polling_ref="poll-batch-1",
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").start_polling()


def test_async_operation_accepts_explicit_infinite_wait_policy() -> None:
    callback_waiting = graphblocks.AsyncOperation.created(
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
        infinite_wait_policy="operator_review_required",
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback()
    polling = graphblocks.AsyncOperation.created(
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
        infinite_wait_policy="provider_has_no_timeout",
    ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").start_polling()

    assert callback_waiting.state == graphblocks.AsyncOperationState.WAITING_CALLBACK
    assert callback_waiting.to_json()["infinite_wait_policy"] == "operator_review_required"
    assert polling.state == graphblocks.AsyncOperationState.POLLING

    with raises_value_error("async operation infinite_wait_policy must not be empty"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-2",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-2",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-2",
            infinite_wait_policy=" ",
        )


def test_async_operation_wait_boundary_deterministic_fuzz() -> None:
    rng = random.Random(6001)

    for case in range(80):
        use_callback = bool(rng.getrandbits(1))
        use_deadline = bool(rng.getrandbits(1))
        use_infinite_policy = bool(rng.getrandbits(1))
        kwargs: dict[str, object] = {
            "operation_id": f"op-wait-{case:03d}",
            "run_id": "run-1",
            "node_id": "waitNode",
            "attempt_id": "attempt-1",
            "kind": "ci_job" if use_callback else "external_provider_job",
            "expected_schema": "schemas/Callback@1",
            "resume_token_hash": "sha256:resume",
            "idempotency_key": f"idem-wait-{case:03d}",
            "created_at": "2026-07-02T00:00:00Z",
        }
        if use_callback:
            kwargs["callback_ref"] = f"cbep-{case:03d}"
        else:
            kwargs["polling_ref"] = f"poll-{case:03d}"
        if use_deadline:
            kwargs["expires_at"] = "2026-07-02T00:30:00Z"
        if use_infinite_policy:
            kwargs["infinite_wait_policy"] = f"explicit-wait-{case:03d}"

        submitted = graphblocks.AsyncOperation.created(**kwargs).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z"
        )
        if use_deadline or use_infinite_policy:
            waiting = submitted.wait_for_callback() if use_callback else submitted.start_polling()
            assert waiting.state in {
                graphblocks.AsyncOperationState.WAITING_CALLBACK,
                graphblocks.AsyncOperationState.POLLING,
            }
        else:
            expected = "callback wait" if use_callback else "polling wait"
            with raises_value_error(f"async operation {expected} requires expires_at or explicit infinite_wait_policy"):
                submitted.wait_for_callback() if use_callback else submitted.start_polling()


def test_async_operation_rejects_invalid_timestamp_format_and_ordering() -> None:
    with raises_value_error("async operation created_at must be an ISO datetime"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash="sha256:resume",
            idempotency_key="idem-ci-1",
            created_at="later",
        )

    with raises_value_error("async operation submitted_at must not be before created_at"):
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
        ).mark_submitted(submitted_at="2026-07-01T23:59:59Z")

    with raises_value_error("async operation completed_at must not be before submitted_at"):
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
            expires_at="2026-07-02T00:30:00Z",
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:00:00Z"
        )

    with raises_value_error("async operation expires_at must be after created_at"):
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
            expires_at="2026-07-02T00:00:00Z",
        )

    with raises_value_error("async operation expires_at must be after submitted_at"):
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
            expires_at="2026-07-02T00:00:02Z",
        ).mark_submitted(submitted_at="2026-07-02T00:00:03Z")


def test_async_operation_rejects_callback_receipt_after_expiry() -> None:
    with raises_value_error("async operation callback receipt must not be after expires_at"):
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
            expires_at="2026-07-02T00:30:00Z",
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:30:01Z"
        )


def test_async_operation_requires_callback_receipt_timestamp() -> None:
    with raises_value_error("async operation callback_received state requires completed_at"):
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
            expires_at="2026-07-02T00:30:00Z",
        ).mark_submitted(submitted_at="2026-07-02T00:00:01Z").wait_for_callback().mark_callback_received()


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
        test_async_operation_result_rejects_duplicate_external_effect_ids,
        test_async_operation_result_rejects_duplicate_provider_effect_ids,
        test_async_operation_result_deep_copies_json_output_and_projection_sequences,
        test_async_operation_result_rejects_non_json_output_and_projection_values,
        test_async_operation_result_projects_from_terminal_operation_state,
        test_async_operation_result_rejects_projection_from_non_terminal_operation,
        test_async_operation_records_callback_wait_metadata_and_state_transitions,
        test_async_operation_records_polling_metadata_and_terminal_failure,
        test_async_operation_rejects_invalid_refs_and_transitions,
        test_async_operation_rejects_state_timestamp_inconsistency,
        test_async_operation_rejects_direct_wait_states_without_required_refs,
        test_async_operation_rejects_direct_unbounded_wait_states,
        test_async_operation_rejects_provider_identity_before_submission,
        test_async_operation_rejects_unbounded_callback_and_polling_waits,
        test_async_operation_accepts_explicit_infinite_wait_policy,
        test_async_operation_wait_boundary_deterministic_fuzz,
        test_async_operation_rejects_invalid_timestamp_format_and_ordering,
        test_async_operation_rejects_callback_receipt_after_expiry,
        test_async_operation_requires_callback_receipt_timestamp,
        test_async_operation_result_exports_are_available,
    )
    for test in tests:
        test()


if __name__ == "__main__":
    run_direct()
