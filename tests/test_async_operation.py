from __future__ import annotations

import re
import math
import random
from collections.abc import Callable
from contextlib import contextmanager

import graphblocks
import pytest


VALID_RESUME_TOKEN_HASH = "sha256:" + "a" * 64


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


def test_async_operation_result_preserves_committed_effect_after_timeout() -> None:
    result = graphblocks.AsyncOperationResult.expired("op-1").with_external_effects(
        [
            graphblocks.ExternalEffectRecord(
                effect_id="effect-batch-1",
                target="batch-provider",
                operation="batch.run",
                outcome="committed",
                idempotency_key="idem-batch-1",
                provider_effect_id="batch-123",
            )
        ]
    )

    assert result.status == "expired"
    assert result.external_effect_was_committed() is True
    assert result.to_json()["external_effects"][0] == {
        "effect_id": "effect-batch-1",
        "target": "batch-provider",
        "operation": "batch.run",
        "outcome": "committed",
        "idempotency_key": "idem-batch-1",
        "provider_effect_id": "batch-123",
    }


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
        graphblocks.AsyncOperationResult.cancelled("op-4").with_external_effects(  # type: ignore[arg-type]
            {"effect_id": "effect-ticket-1"}
        )

    with raises_value_error("async operation result external_effects must be a sequence"):
        graphblocks.AsyncOperationResult.cancelled("op-4").with_external_effects(b"effect-ticket-1")  # type: ignore[arg-type]

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


def test_async_operation_result_freezes_internal_json_mappings() -> None:
    result = graphblocks.AsyncOperationResult.completed(
        "op-1",
        output={"summary": {"passed": True, "checks": ["lint"]}},
    ).with_projections(artifacts=[{"artifact_id": "artifact-1", "uri": "blob://ci/log"}])

    assert result.output["summary"]["checks"] == ("lint",)  # type: ignore[index]
    with pytest.raises(TypeError):
        result.output["summary"] = {"passed": False}  # type: ignore[index]
    with pytest.raises(TypeError):
        result.output["summary"]["passed"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        result.artifacts[0]["uri"] = "blob://ci/mutated"  # type: ignore[index]


def test_async_operation_result_rejects_non_json_output_and_projection_values() -> None:
    with raises_value_error("async operation result output must contain only JSON values"):
        graphblocks.AsyncOperationResult.completed("op-1", output=object())

    with raises_value_error("async operation result output must not contain non-finite numbers"):
        graphblocks.AsyncOperationResult.completed("op-1", output={"value": math.nan})

    with raises_value_error("async operation result output must contain only JSON values"):
        graphblocks.AsyncOperationResult.completed("op-1", output=("not", "json"))

    with raises_value_error("async operation result artifacts must contain only JSON values"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(artifacts=[{"bad": object()}])

    with raises_value_error("async operation result diagnostics must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(
            diagnostics={"code": "callback.schema_mismatch"}  # type: ignore[arg-type]
        )

    with raises_value_error("async operation result metrics must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(metrics=object())  # type: ignore[arg-type]

    with raises_value_error("async operation result checks must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(checks="unit")  # type: ignore[arg-type]

    with raises_value_error("async operation result usage must be a sequence"):
        graphblocks.AsyncOperationResult.completed("op-1").with_projections(usage=b"usage")  # type: ignore[arg-type]

    for field_name, projection in (
        ("diagnostics", {"diagnostics": ["warning"]}),
        ("metrics", {"metrics": [42]}),
        ("checks", {"checks": [True]}),
        ("usage", {"usage": ["tokens"]}),
    ):
        with raises_value_error(f"async operation result {field_name} entries must be JSON objects"):
            graphblocks.AsyncOperationResult.completed("op-1").with_projections(**projection)  # type: ignore[arg-type]


def test_async_operation_result_projects_from_terminal_operation_state() -> None:
    completed_operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    ).wait_for_callback().mark_callback_received(
        completed_at="2026-07-02T00:10:00Z"
    ).mark_resuming().complete(completed_at="2026-07-02T00:10:05Z")
    cancelled_operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-2",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
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


def test_async_operation_result_projects_late_callback_as_incomplete_diagnostic() -> None:
    cancelled_operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-late-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-late-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-late-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    ).wait_for_callback().cancel(
        completed_at="2026-07-02T00:05:00Z"
    )
    committed_effect = graphblocks.ExternalEffectRecord(
        effect_id="effect-ci-1",
        target="github-actions",
        operation="workflow_dispatch",
        outcome="committed",
        idempotency_key="idem-ci-late-1",
        provider_effect_id="gha-run-1",
    )

    late_result = graphblocks.AsyncOperationResult.from_late_callback(
        cancelled_operation,
        output={"status": "completed", "late": True},
        diagnostics=[{"code": "late_callback", "message": "callback arrived after cancellation"}],
        external_effects=[committed_effect],
    )

    assert late_result.operation_id == "op-ci-late-1"
    assert late_result.status == graphblocks.AsyncOperationResultStatus.INCOMPLETE
    assert late_result.output == {"status": "completed", "late": True}
    assert late_result.diagnostics == (
        {"code": "late_callback", "message": "callback arrived after cancellation"},
    )
    assert late_result.external_effect_was_committed() is True
    assert late_result.to_json()["external_effects"][0]["provider_effect_id"] == "gha-run-1"


def test_external_callback_received_schema_freezes_payload_and_artifacts() -> None:
    payload = {"status": "completed", "checks": ["lint"]}
    artifacts = [{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}]

    receipt = graphblocks.ExternalCallbackReceived(
        callback_id="cb-1",
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        idempotency_key="idem-callback-1",
        payload=payload,
        payload_digest=graphblocks.canonical_hash(payload),
        received_at="2026-07-02T00:10:00Z",
        verified_by="hmac-sha256:callback-endpoint-1",
        policy_snapshot_id="policy-1",
        provider_operation_id="gha-run-1",
        artifacts=artifacts,
    )

    payload["checks"].append("unit")  # type: ignore[index, union-attr]
    artifacts[0]["uri"] = "blob://ci/mutated"
    projected = receipt.to_json()
    projected["payload"]["checks"].append("caller-mutation")  # type: ignore[index]
    projected["artifacts"][0]["uri"] = "blob://ci/caller-mutation"  # type: ignore[index]

    assert receipt.payload == {"status": "completed", "checks": ("lint",)}
    assert receipt.artifacts == ({"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"},)
    assert receipt.to_json() == {
        "callback_id": "cb-1",
        "operation_id": "op-ci-1",
        "run_id": "run-1",
        "node_id": "startCI",
        "attempt_id": "attempt-1",
        "provider_operation_id": "gha-run-1",
        "idempotency_key": "idem-callback-1",
        "payload": {"status": "completed", "checks": ["lint"]},
        "payload_digest": graphblocks.canonical_hash({"status": "completed", "checks": ["lint"]}),
        "artifacts": [{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}],
        "received_at": "2026-07-02T00:10:00Z",
        "verified_by": "hmac-sha256:callback-endpoint-1",
        "policy_snapshot_id": "policy-1",
    }


def test_external_callback_received_freezes_internal_payload_and_artifacts() -> None:
    payload = {"status": "completed", "checks": ["lint"]}
    receipt = graphblocks.ExternalCallbackReceived(
        callback_id="cb-1",
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        idempotency_key="idem-callback-1",
        payload=payload,
        payload_digest=graphblocks.canonical_hash(payload),
        received_at="2026-07-02T00:10:00Z",
        verified_by="hmac-sha256:callback-endpoint-1",
        policy_snapshot_id="policy-1",
        artifacts=[{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}],
    )

    assert receipt.payload["checks"] == ("lint",)  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.payload["status"] = "failed"  # type: ignore[index]
    with pytest.raises(TypeError):
        receipt.artifacts[0]["uri"] = "blob://ci/mutated"  # type: ignore[index]


def test_external_callback_received_accepts_camel_case_artifacts() -> None:
    receipt = graphblocks.ExternalCallbackReceived(
        callback_id="cb-1",
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        idempotency_key="idem-callback-1",
        payload={"status": "completed"},
        payload_digest=graphblocks.canonical_hash({"status": "completed"}),
        received_at="2026-07-02T00:10:00Z",
        verified_by="hmac-sha256:callback-endpoint-1",
        policy_snapshot_id="policy-1",
        artifacts=[
            {
                "artifactId": "artifact-ci-log",
                "uri": "blob://ci/log",
                "mediaType": "application/json",
                "sizeBytes": 128,
            }
        ],
    )

    assert receipt.artifacts == (
        {
            "artifact_id": "artifact-ci-log",
            "uri": "blob://ci/log",
            "media_type": "application/json",
            "size_bytes": 128,
        },
    )
    assert receipt.to_json()["artifacts"] == [
        {
            "artifact_id": "artifact-ci-log",
            "uri": "blob://ci/log",
            "media_type": "application/json",
            "size_bytes": 128,
        }
    ]


def test_external_callback_received_rejects_invalid_identity_digest_and_json() -> None:
    with raises_value_error("external callback received callback_id must not be empty"):
        graphblocks.ExternalCallbackReceived(
            callback_id=" ",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
        )

    with raises_value_error("external callback received payload_digest must be a canonical sha256 digest"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest="sha256:not-a-digest",
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
        )

    with raises_value_error("external callback received payload_digest must match payload"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "failed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
        )

    with raises_value_error("external callback received received_at must be an ISO datetime"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="not-a-date",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
        )

    with raises_value_error("external callback received payload must contain only JSON values"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"bad": object()},
            payload_digest="sha256:" + "b" * 64,
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
        )

    with raises_value_error("external callback received artifacts must be a sequence"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts={"artifact_id": "artifact-ci-log"},  # type: ignore[arg-type]
        )

    with raises_value_error("external callback received artifacts entries must be JSON objects"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts=["artifact-ci-log"],  # type: ignore[list-item]
        )

    with raises_value_error("external callback received artifacts uri must be a non-empty string"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts=[{"artifact_id": "artifact-ci-log"}],
        )

    with raises_value_error("external callback received artifacts media_type must be a non-empty string"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts=[{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "media_type": " "}],
        )

    with raises_value_error("external callback received artifacts checksum must be a non-empty string"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts=[{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "checksum": ""}],
        )

    for artifact in (
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "size_bytes": True},
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "size_bytes": -1},
        {"artifactId": "artifact-ci-log", "uri": "blob://ci/log", "sizeBytes": "128"},
    ):
        with raises_value_error("external callback received artifacts size_bytes must be a non-negative integer"):
            graphblocks.ExternalCallbackReceived(
                callback_id="cb-1",
                operation_id="op-ci-1",
                run_id="run-1",
                node_id="startCI",
                attempt_id="attempt-1",
                idempotency_key="idem-callback-1",
                payload={"status": "completed"},
                payload_digest=graphblocks.canonical_hash({"status": "completed"}),
                received_at="2026-07-02T00:10:00Z",
                verified_by="hmac-sha256:callback-endpoint-1",
                policy_snapshot_id="policy-1",
                artifacts=[artifact],
            )

    with raises_value_error("external callback received artifacts must not contain duplicate artifact_id"):
        graphblocks.ExternalCallbackReceived(
            callback_id="cb-1",
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=graphblocks.canonical_hash({"status": "completed"}),
            received_at="2026-07-02T00:10:00Z",
            verified_by="hmac-sha256:callback-endpoint-1",
            policy_snapshot_id="policy-1",
            artifacts=[
                {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log-1"},
                {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log-2"},
            ],
        )


def test_async_operation_result_rejects_projection_from_non_terminal_operation() -> None:
    waiting = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    ).wait_for_callback()

    with raises_value_error("async operation result requires a terminal operation"):
        graphblocks.AsyncOperationResult.from_operation(waiting)

    with raises_value_error("late callback result requires a terminal operation"):
        graphblocks.AsyncOperationResult.from_late_callback(waiting)


def test_async_operation_requires_resume_token_hash_digest() -> None:
    for invalid_hash in ("sha256:resume", "a" * 64):
        with raises_value_error("async operation resume_token_hash must be a canonical sha256 digest"):
            graphblocks.AsyncOperation.created(
                operation_id="op-ci-1",
                run_id="run-1",
                node_id="startCI",
                attempt_id="attempt-1",
                kind="ci_job",
                expected_schema="schemas/CICallback@1",
                resume_token_hash=invalid_hash,
                idempotency_key="idem-ci-1",
                created_at="2026-07-02T00:00:00Z",
                callback_ref="cbep-ci-1",
            )

    assert graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).resume_token_hash == VALID_RESUME_TOKEN_HASH


def test_async_operation_records_callback_wait_metadata_and_state_transitions() -> None:
    operation = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    )

    submitted = operation.mark_submitted(
        provider_operation_id="gha-run-1",
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    )
    waiting = submitted.wait_for_callback()
    received = waiting.mark_callback_received(callback_received_at="2026-07-02T00:10:00Z")
    resuming = received.mark_resuming()
    completed = resuming.complete(completed_at="2026-07-02T00:10:05Z")

    assert operation.state == graphblocks.AsyncOperationState.CREATED
    assert submitted.state == graphblocks.AsyncOperationState.SUBMITTED
    assert submitted.provider_operation_id == "gha-run-1"
    assert waiting.state == graphblocks.AsyncOperationState.WAITING_CALLBACK
    assert received.state == graphblocks.AsyncOperationState.CALLBACK_RECEIVED
    assert received.callback_received_at == "2026-07-02T00:10:00Z"
    assert received.completed_at is None
    assert resuming.state == graphblocks.AsyncOperationState.RESUMING
    assert completed.state == graphblocks.AsyncOperationState.COMPLETED
    assert completed.callback_received_at == "2026-07-02T00:10:00Z"
    assert completed.completed_at == "2026-07-02T00:10:05Z"
    assert completed.to_json()["callback_received_at"] == "2026-07-02T00:10:00Z"
    assert completed.to_json()["callback_ref"] == "cbep-ci-1"


def test_async_operation_rejects_conflicting_callback_receipt_aliases() -> None:
    waiting = graphblocks.AsyncOperation.created(
        operation_id="op-ci-1",
        run_id="run-1",
        node_id="startCI",
        attempt_id="attempt-1",
        kind="ci_job",
        expected_schema="schemas/CICallback@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    ).wait_for_callback()

    with raises_value_error("async operation callback_received_at and completed_at alias must match"):
        waiting.mark_callback_received(
            callback_received_at="2026-07-02T00:10:00Z",
            completed_at="2026-07-02T00:10:01Z",
        )

    with raises_value_error("async operation callback_received state must not have completed_at"):
        graphblocks.AsyncOperation(
            operation_id="op-ci-2",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            state="callback_received",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-2",
            created_at="2026-07-02T00:00:00Z",
            submitted_at="2026-07-02T00:00:01Z",
            callback_ref="cbep-ci-2",
            expires_at="2026-07-02T00:30:00Z",
            callback_received_at="2026-07-02T00:10:00Z",
            completed_at="2026-07-02T00:10:00Z",
        )


def test_async_operation_records_polling_metadata_and_terminal_failure() -> None:
    operation = graphblocks.AsyncOperation.created(
        operation_id="op-batch-1",
        run_id="run-1",
        node_id="waitBatch",
        attempt_id="attempt-1",
        kind="external_provider_job",
        expected_schema="schemas/BatchResult@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-batch-1",
        created_at="2026-07-02T00:00:00Z",
        polling_ref="poll-batch-1",
    )

    failed = operation.mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T02:00:00Z",
    ).start_polling().fail(
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        expires_at="2026-07-02T00:30:00Z",
    ).wait_for_callback().mark_callback_received(
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            completed_at="2026-07-02T00:10:05Z",
        )

    for wait_boundary in (
        {"expires_at": "2026-07-02T00:30:00Z"},
        {"infinite_wait_policy": "operator_review_required"},
    ):
        with raises_value_error("async operation created state must not have wait boundary"):
            graphblocks.AsyncOperation(
                operation_id="op-ci-1",
                run_id="run-1",
                node_id="startCI",
                attempt_id="attempt-1",
                kind="ci_job",
                state="created",
                expected_schema="schemas/CICallback@1",
                resume_token_hash=VALID_RESUME_TOKEN_HASH,
                idempotency_key="idem-ci-1",
                created_at="2026-07-02T00:00:00Z",
                **wait_boundary,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            provider_operation_id="gha-run-1",
        )


def test_async_operation_rejects_ambiguous_callback_and_polling_refs() -> None:
    with raises_value_error("async operation must not define both callback_ref and polling_ref"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ambiguous-wait-1",
            run_id="run-1",
            node_id="waitExternal",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/ExternalResult@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ambiguous-wait-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ambiguous-1",
            polling_ref="poll-ambiguous-1",
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-ci-1",
        created_at="2026-07-02T00:00:00Z",
        callback_ref="cbep-ci-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        infinite_wait_policy="operator_review_required",
    ).wait_for_callback()
    polling = graphblocks.AsyncOperation.created(
        operation_id="op-batch-1",
        run_id="run-1",
        node_id="waitBatch",
        attempt_id="attempt-1",
        kind="external_provider_job",
        expected_schema="schemas/BatchResult@1",
        resume_token_hash=VALID_RESUME_TOKEN_HASH,
        idempotency_key="idem-batch-1",
        created_at="2026-07-02T00:00:00Z",
        polling_ref="poll-batch-1",
    ).mark_submitted(
        submitted_at="2026-07-02T00:00:01Z",
        infinite_wait_policy="provider_has_no_timeout",
    ).start_polling()

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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-2",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-2",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            infinite_wait_policy=" ",
        )


def test_async_operation_rejects_ambiguous_deadline_and_infinite_wait_policy() -> None:
    with raises_value_error(
        "async operation wait must not define both expires_at and infinite_wait_policy"
    ):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-ambiguous-wait",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-ambiguous-wait",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-ambiguous",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
            infinite_wait_policy="operator_review_required",
        ).wait_for_callback()

    with raises_value_error(
        "async operation wait must not define both expires_at and infinite_wait_policy"
    ):
        graphblocks.AsyncOperation.created(
            operation_id="op-batch-ambiguous-wait",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-batch-ambiguous-wait",
            created_at="2026-07-02T00:00:00Z",
            polling_ref="poll-batch-ambiguous",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
            infinite_wait_policy="provider_has_no_timeout",
        ).start_polling()


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
            "resume_token_hash": VALID_RESUME_TOKEN_HASH,
            "idempotency_key": f"idem-wait-{case:03d}",
            "created_at": "2026-07-02T00:00:00Z",
        }
        if use_callback:
            kwargs["callback_ref"] = f"cbep-{case:03d}"
        else:
            kwargs["polling_ref"] = f"poll-{case:03d}"
        wait_kwargs: dict[str, object] = {}
        if use_deadline:
            wait_kwargs["expires_at"] = "2026-07-02T00:30:00Z"
        if use_infinite_policy:
            wait_kwargs["infinite_wait_policy"] = f"explicit-wait-{case:03d}"

        submitted = graphblocks.AsyncOperation.created(**kwargs).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            **wait_kwargs,
        )
        if use_deadline and use_infinite_policy:
            with raises_value_error(
                "async operation wait must not define both expires_at and infinite_wait_policy"
            ):
                submitted.wait_for_callback() if use_callback else submitted.start_polling()
        elif use_deadline or use_infinite_policy:
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
        ).mark_submitted(submitted_at="2026-07-01T23:59:59Z")

    with raises_value_error("async operation callback_received_at must not be before submitted_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received(
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
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
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:03Z",
            expires_at="2026-07-02T00:00:02Z",
        )


def test_async_operation_rejects_callback_receipt_after_expiry() -> None:
    with raises_value_error("async operation callback receipt must not be after expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:30:01Z"
        )


def test_async_operation_rejects_polling_completion_after_expiry() -> None:
    with raises_value_error("async operation polling completion must not be after expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-batch-1",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-batch-1",
            created_at="2026-07-02T00:00:00Z",
            polling_ref="poll-batch-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).start_polling().complete(
            completed_at="2026-07-02T00:30:01Z"
        )


def test_async_operation_rejects_callback_completion_after_expiry() -> None:
    with raises_value_error("async operation callback completion must not be after expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:29:59Z"
        ).mark_resuming().complete(completed_at="2026-07-02T00:30:01Z")


def test_async_operation_rejects_terminal_transition_before_callback_receipt() -> None:
    with raises_value_error("async operation terminal completed_at must not be before callback receipt"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:10:00Z"
        ).mark_resuming().complete(completed_at="2026-07-02T00:09:59Z")


def test_async_operation_callback_terminal_ordering_deterministic_fuzz() -> None:
    rng = random.Random(6017)
    terminals: tuple[tuple[str, Callable[[graphblocks.AsyncOperation, str], graphblocks.AsyncOperation]], ...] = (
        ("completed", lambda operation, completed_at: operation.complete(completed_at=completed_at)),
        ("failed", lambda operation, completed_at: operation.fail(completed_at=completed_at)),
        ("cancelled", lambda operation, completed_at: operation.cancel(completed_at=completed_at)),
        ("expired", lambda operation, completed_at: operation.expire(completed_at=completed_at)),
    )

    for case in range(64):
        terminal_name, transition = terminals[rng.randrange(len(terminals))]
        receipt_second = 5 + rng.randrange(20)
        terminal_delta = rng.randrange(-4, 5)
        terminal_second = receipt_second + terminal_delta
        terminal_at = f"2026-07-02T00:00:{terminal_second:02d}Z"
        expires_at = "2026-07-02T00:30:00Z"
        if terminal_name == "expired":
            expires_at = terminal_at if terminal_delta >= 0 else f"2026-07-02T00:00:{receipt_second:02d}Z"
        received = graphblocks.AsyncOperation.created(
            operation_id=f"op-ci-{case}",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key=f"idem-ci-{case}",
            created_at="2026-07-02T00:00:00Z",
            callback_ref=f"cbep-ci-{case}",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at=expires_at,
        ).wait_for_callback().mark_callback_received(
            completed_at=f"2026-07-02T00:00:{receipt_second:02d}Z"
        ).mark_resuming()

        if terminal_delta < 0:
            with raises_value_error("async operation terminal completed_at must not be before callback receipt"):
                transition(received, terminal_at)
        else:
            terminal = transition(received, terminal_at)
            assert terminal.state == terminal_name
            assert terminal.completed_at == terminal_at


def test_async_operation_rejects_expiry_before_deadline() -> None:
    with raises_value_error("async operation expired completed_at must not be before expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-expire-early",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-expire-early",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-expire-early",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().expire(
            completed_at="2026-07-02T00:29:59Z"
        )

    with raises_value_error("async operation expired completed_at must not be before expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-batch-expire-early",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-batch-expire-early",
            created_at="2026-07-02T00:00:00Z",
            polling_ref="poll-batch-expire-early",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).start_polling().expire(
            completed_at="2026-07-02T00:29:59Z"
        )


def test_async_operation_rejects_terminal_failure_after_expiry() -> None:
    with raises_value_error("async operation polling failure must not be after expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-batch-1",
            run_id="run-1",
            node_id="waitBatch",
            attempt_id="attempt-1",
            kind="external_provider_job",
            expected_schema="schemas/BatchResult@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-batch-1",
            created_at="2026-07-02T00:00:00Z",
            polling_ref="poll-batch-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).start_polling().fail(
            completed_at="2026-07-02T00:30:01Z"
        )

    with raises_value_error("async operation callback failure must not be after expires_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received(
            completed_at="2026-07-02T00:29:59Z"
        ).mark_resuming().fail(completed_at="2026-07-02T00:30:01Z")


def test_async_operation_requires_callback_receipt_timestamp() -> None:
    with raises_value_error("async operation callback_received state requires callback_received_at"):
        graphblocks.AsyncOperation.created(
            operation_id="op-ci-1",
            run_id="run-1",
            node_id="startCI",
            attempt_id="attempt-1",
            kind="ci_job",
            expected_schema="schemas/CICallback@1",
            resume_token_hash=VALID_RESUME_TOKEN_HASH,
            idempotency_key="idem-ci-1",
            created_at="2026-07-02T00:00:00Z",
            callback_ref="cbep-ci-1",
        ).mark_submitted(
            submitted_at="2026-07-02T00:00:01Z",
            expires_at="2026-07-02T00:30:00Z",
        ).wait_for_callback().mark_callback_received()


def test_async_operation_result_exports_are_available() -> None:
    assert "AsyncOperation" in graphblocks.__all__
    assert "AsyncOperationState" in graphblocks.__all__
    assert "AsyncOperationResult" in graphblocks.__all__
    assert "ExternalEffectRecord" in graphblocks.__all__
    assert "ExternalCallbackReceived" in graphblocks.__all__
    assert graphblocks.AsyncOperationState.WAITING_CALLBACK == "waiting_callback"
    assert graphblocks.AsyncOperationResultStatus.CANCELLED == "cancelled"


def run_direct() -> None:
    tests: tuple[Callable[[], None], ...] = (
        test_async_operation_result_preserves_committed_effect_after_cancel,
        test_async_operation_result_preserves_committed_effect_after_incomplete_late_callback,
        test_async_operation_result_preserves_committed_effect_after_timeout,
        test_async_operation_result_rejects_invalid_external_effect_records,
        test_async_operation_result_rejects_duplicate_external_effect_ids,
        test_async_operation_result_rejects_duplicate_provider_effect_ids,
        test_async_operation_result_deep_copies_json_output_and_projection_sequences,
        test_async_operation_result_rejects_non_json_output_and_projection_values,
        test_async_operation_result_projects_from_terminal_operation_state,
        test_async_operation_result_projects_late_callback_as_incomplete_diagnostic,
        test_async_operation_result_rejects_projection_from_non_terminal_operation,
        test_async_operation_requires_resume_token_hash_digest,
        test_async_operation_records_callback_wait_metadata_and_state_transitions,
        test_async_operation_rejects_conflicting_callback_receipt_aliases,
        test_async_operation_records_polling_metadata_and_terminal_failure,
        test_async_operation_rejects_invalid_refs_and_transitions,
        test_async_operation_rejects_state_timestamp_inconsistency,
        test_async_operation_rejects_direct_wait_states_without_required_refs,
        test_async_operation_rejects_direct_unbounded_wait_states,
        test_async_operation_rejects_provider_identity_before_submission,
        test_async_operation_rejects_ambiguous_callback_and_polling_refs,
        test_async_operation_rejects_unbounded_callback_and_polling_waits,
        test_async_operation_accepts_explicit_infinite_wait_policy,
        test_async_operation_rejects_ambiguous_deadline_and_infinite_wait_policy,
        test_async_operation_wait_boundary_deterministic_fuzz,
        test_async_operation_rejects_invalid_timestamp_format_and_ordering,
        test_async_operation_rejects_callback_receipt_after_expiry,
        test_async_operation_rejects_polling_completion_after_expiry,
        test_async_operation_rejects_callback_completion_after_expiry,
        test_async_operation_rejects_terminal_transition_before_callback_receipt,
        test_async_operation_callback_terminal_ordering_deterministic_fuzz,
        test_async_operation_rejects_expiry_before_deadline,
        test_async_operation_rejects_terminal_failure_after_expiry,
        test_async_operation_requires_callback_receipt_timestamp,
        test_async_operation_result_exports_are_available,
    )
    for test in tests:
        test()


if __name__ == "__main__":
    run_direct()
