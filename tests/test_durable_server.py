from __future__ import annotations

from collections.abc import Callable, Mapping
from multiprocessing import active_children
import os
from pathlib import Path
from textwrap import dedent

import pytest

import graphblocks.durable_worker as durable_worker_module
from graphblocks.canonical import canonical_dumps, canonical_hash, canonical_loads
from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_registry import is_durable_intent_registry
from graphblocks.durable_server import DurableAcceptedRunService
from graphblocks.durable_worker import DEFAULT_DURABLE_WORKER_TARGET
from graphblocks.isolated_worker import (
    ProcessWorkerDeadlineExceeded,
    ProcessWorkerPolicy,
    ProcessWorkerProtocolError,
    ProcessWorkerTarget,
)
from graphblocks.runtime import stdlib_registry
from graphblocks.server_storage import (
    AcceptedRunCallbackCommit,
    AcceptedRunClaimRequest,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEventIntent,
    AcceptedRunPhase,
    CallbackIssuanceIdentity,
    CallbackSubmissionIdentity,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_RESUME_TOKEN_HASH = "sha256:" + ("a" * 64)


def _fixed_clock(value: int) -> Callable[[], int]:
    def clock() -> int:
        return value

    return clock


def _service(
    path: Path,
    *,
    worker_id: str,
    clock: Callable[[], int],
) -> DurableAcceptedRunService:
    return DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(path, clock=clock),
        lease_owner_id=worker_id,
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
    )


def _write_durable_worker_fixture(path: Path) -> str:
    module_name = "durable_worker_fixture"
    (path / f"{module_name}.py").write_text(
        dedent(
            """
            import os

            from graphblocks.worker import WorkerInvokeResult


            def succeed(request):
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch,
                    outputs={
                        "authorityDigest": request.config["authorityDigest"],
                        "runtimeResult": {
                            "checkpoint": None,
                            "outputs": {"workerPid": os.getpid()},
                            "runId": request.run_id,
                            "status": "succeeded",
                        },
                    },
                )


            def tamper_authority(request):
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch,
                    outputs={
                        "authorityDigest": "sha256:" + ("0" * 64),
                        "runtimeResult": {
                            "checkpoint": None,
                            "outputs": {},
                            "runId": request.run_id,
                            "status": "succeeded",
                        },
                    },
                )


            def spin_forever(request):
                del request
                while True:
                    pass
            """
        ),
        encoding="utf-8",
    )
    return module_name


def _admit_empty_run(
    service: DurableAcceptedRunService,
    *,
    run_id: str,
) -> None:
    service.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id=run_id,
        idempotency_key=f"request-{run_id}",
        graph={
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": f"isolated-{run_id}"},
            "spec": {"nodes": {}},
        },
        inputs={},
        invocation={
            "policySnapshotId": "policy-1",
            "releaseId": "release-1",
            "responseId": "response-1",
            "turnId": None,
        },
        created_at_unix_ms=1_000,
    )


def test_durable_service_executes_admitted_run_after_process_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-restart"},
        "spec": {"nodes": {}},
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    admitted = _service(
        path,
        worker_id="admission-process",
        clock=lambda: 1_000,
    ).admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="run-1",
        idempotency_key="request-1",
        graph=graph,
        inputs={},
        invocation=invocation,
    )

    restarted = _service(
        path,
        worker_id="worker-after-restart",
        clock=lambda: 2_000,
    )
    replay = restarted.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="run-1",
        idempotency_key="request-1",
        graph=graph,
        inputs={},
        invocation=invocation,
        created_at_unix_ms=1_000,
    )
    completed = restarted.advance_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    duplicate = restarted.advance_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )

    assert not admitted.replayed
    assert replay.replayed
    assert completed.phase is AcceptedRunPhase.TERMINAL
    assert completed.terminal_status == "succeeded"
    assert canonical_loads(completed.terminal_result_json) == {
        "outputs": {},
        "status": "succeeded",
    }
    assert duplicate == completed
    events = restarted.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
        "run_succeeded",
    ]


def test_default_durable_parent_and_child_share_intent_registry(
    tmp_path: Path,
) -> None:
    clock = _fixed_clock(2_000)
    service = _service(
        tmp_path / "durable-intent-parent-child.sqlite3",
        worker_id="worker-1",
        clock=clock,
    )
    assert is_durable_intent_registry(service.registry)
    service.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="run-intent-parent-child",
        idempotency_key="request-intent-parent-child",
        graph={
            "apiVersion": "graphblocks.ai/v1",
            "kind": "Graph",
            "metadata": {"name": "durable-intent-parent-child"},
            "spec": {
                "interface": {
                    "inputs": {"message": "graphblocks.ai/Message@1"},
                    "outputs": {"prompt": "graphblocks.ai/Prompt@1"},
                },
                "nodes": {
                    "render": {
                        "block": "prompt.render@1",
                        "config": {"template": "Echo {message.text}"},
                        "inputs": {"message": "$input.message"},
                        "outputs": {"prompt": "$output.prompt"},
                    }
                },
            },
        },
        inputs={"message": {"text": "hello"}},
        invocation={
            "policySnapshotId": "policy-1",
            "releaseId": "release-1",
            "responseId": "response-1",
            "turnId": None,
        },
    )

    completed = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-intent-parent-child",
    )

    assert completed.terminal_result_json is not None
    assert canonical_loads(completed.terminal_result_json) == {
        "outputs": {"prompt": "Echo hello"},
        "status": "succeeded",
    }


def test_durable_child_entrypoint_reconstructs_intent_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _fixed_clock(2_000)
    service = _service(
        tmp_path / "durable-intent-child-entrypoint.sqlite3",
        worker_id="worker-1",
        clock=clock,
    )
    _admit_empty_run(service, run_id="run-intent-child-entrypoint")
    work = service.repository.claim_work(
        AcceptedRunClaimRequest(
            tenant_id="tenant-1",
            run_id="run-intent-child-entrypoint",
            lease_owner_id="worker-1",
            now_unix_ms=2_000,
            lease_duration_ms=30_000,
        )
    )
    assert work is not None
    graph = canonical_loads(work.envelope.graph_json)
    inputs = canonical_loads(work.envelope.inputs_json)
    assert isinstance(graph, dict)
    assert isinstance(inputs, dict)
    request = durable_worker_module.build_durable_worker_request(
        work,
        graph=graph,
        inputs=inputs,
    )
    factory_calls = 0
    intent_registry_factory = durable_worker_module.durable_intent_registry

    def tracked_intent_registry_factory():
        nonlocal factory_calls
        factory_calls += 1
        return intent_registry_factory()

    monkeypatch.setattr(
        durable_worker_module,
        "durable_intent_registry",
        tracked_intent_registry_factory,
    )

    result = durable_worker_module.execute_durable_worker_request(request)

    assert factory_calls == 1
    runtime_result = result.outputs["runtimeResult"]
    assert isinstance(runtime_result, Mapping)
    assert runtime_result["status"] == "succeeded"


def test_durable_service_executes_next_tenant_scoped_run_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server-next.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-next"},
        "spec": {"nodes": {}},
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    admission_process = _service(
        path,
        worker_id="admission-process",
        clock=lambda: 1_000,
    )
    admission_process.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="tenant-1-run",
        idempotency_key="tenant-1-request",
        graph=graph,
        inputs={},
        invocation=invocation,
    )
    admission_process.admit_run(
        tenant_id="tenant-2",
        owner_principal_id="principal-2",
        run_id="tenant-2-run",
        idempotency_key="tenant-2-request",
        graph=graph,
        inputs={},
        invocation=invocation,
    )

    restarted = _service(
        path,
        worker_id="worker-after-restart",
        clock=lambda: 2_000,
    )
    tenant_work = restarted.advance_next_run(tenant_id="tenant-2")
    remaining_work = restarted.advance_next_run()
    no_work = restarted.advance_next_run()

    assert tenant_work is not None
    assert tenant_work.tenant_id == "tenant-2"
    assert tenant_work.run_id == "tenant-2-run"
    assert tenant_work.phase is AcceptedRunPhase.TERMINAL
    assert remaining_work is not None
    assert remaining_work.tenant_id == "tenant-1"
    assert remaining_work.run_id == "tenant-1-run"
    assert remaining_work.phase is AcceptedRunPhase.TERMINAL
    assert no_work is None


def test_durable_service_resumes_accepted_callback_after_process_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server-callback.sqlite3"
    run_id = "run-callback-1"
    operation_id = "operation-callback-1"
    operation_idempotency_key = "operation-idempotency-1"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-callback"},
        "spec": {
            "nodes": {
                "start": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": operation_id,
                        "runId": run_id,
                        "nodeId": "wait",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-operation-1",
                        "resumeTokenHash": _RESUME_TOKEN_HASH,
                        "idempotencyKey": operation_idempotency_key,
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeoutMs": 60_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                },
                "wait": {
                    "block": "async.await_callback@1",
                    "inputs": {"operation": "start.operation"},
                    "config": {
                        "checkpoint": True,
                        "onTimeout": "fail",
                        "timeoutMs": 60_000,
                        "idempotencyKey": operation_idempotency_key,
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                },
            }
        },
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    first_process = _service(
        path,
        worker_id="worker-before-callback",
        clock=lambda: 2_000,
    )
    first_process.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id=run_id,
        idempotency_key="request-callback-1",
        graph=graph,
        inputs={},
        invocation=invocation,
        created_at_unix_ms=1_000,
    )
    waiting = first_process.advance_run(
        tenant_id="tenant-1",
        run_id=run_id,
    )

    assert waiting.phase is AcceptedRunPhase.WAITING_CALLBACK
    dispatcher = SQLiteOutboxDispatcherRepository(path)
    dispatch = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="operation-dispatcher",
            now_unix_ms=2_100,
            lease_duration_ms=5_000,
        )
    )
    assert dispatch is not None
    dispatch_payload = canonical_loads(dispatch.payload_json)
    assert isinstance(dispatch_payload, dict)
    issuance_payload = dispatch_payload["callbackIssuance"]
    assert isinstance(issuance_payload, dict)
    issuance = CallbackIssuanceIdentity(
        run_id=str(issuance_payload["runId"]),
        checkpoint_digest=str(issuance_payload["checkpointDigest"]),
        operation_id=str(issuance_payload["operationId"]),
        operation_attempt_id=str(issuance_payload["operationAttemptId"]),
        callback_idempotency_key=str(issuance_payload["callbackIdempotencyKey"]),
        lease_generation=int(issuance_payload["leaseGeneration"]),
        fencing_token=int(issuance_payload["fencingToken"]),
    )
    callback_payload = {"status": "completed"}
    receipt = {
        "operation_id": operation_id,
        "run_id": run_id,
        "node_id": "wait",
        "attempt_id": "attempt-1",
        "provider_operation_id": "provider-operation-1",
        "operation_idempotency_key": operation_idempotency_key,
        "callback_idempotency_key": issuance.callback_idempotency_key,
        "resume_token_hash": _RESUME_TOKEN_HASH,
        "schema_id": "schemas/CICallback@1",
        "schema_validated": True,
        "payload": callback_payload,
        "payload_digest": canonical_hash(callback_payload),
        "received_at_unix_ms": 3_000,
        "verified_by": "callback-relay",
        "resume_admission": {
            "policy_reevaluated": True,
            "budget_reserved": True,
            "release_compatible": True,
            "ownership_fenced": True,
        },
    }
    accepted_event_payload = {
        "checkpointDigest": issuance.checkpoint_digest,
        "resumeState": "ready_resume",
        "runId": run_id,
    }
    acceptance = first_process.accept_callback(
        AcceptedRunCallbackCommit(
            tenant_id="tenant-1",
            owner_principal_id="principal-1",
            expected_state_version=waiting.state_version,
            submission=CallbackSubmissionIdentity(
                issuance=issuance,
                payload_digest=canonical_hash(callback_payload),
            ),
            payload_json=canonical_dumps(callback_payload),
            receipt_json=canonical_dumps(receipt),
            received_at_unix_ms=3_000,
            accepted_event=AcceptedRunEventIntent(
                kind="external_callback_received",
                payload_json=canonical_dumps(accepted_event_payload),
                payload_digest=canonical_hash(accepted_event_payload),
                created_at_unix_ms=3_000,
            ),
        )
    )

    assert acceptance.state_version == 4
    settled_dispatch = dispatcher.get_effect(effect_id=dispatch.effect_id)
    assert settled_dispatch is not None
    assert (
        settled_dispatch.delivery_state
        is AcceptedRunEffectDeliveryState.SATISFIED_BY_CALLBACK
    )

    restarted = _service(
        path,
        worker_id="worker-after-callback",
        clock=lambda: 4_000,
    )
    completed = restarted.advance_next_run(tenant_id="tenant-1")

    assert completed is not None
    assert completed.phase is AcceptedRunPhase.TERMINAL
    assert completed.terminal_status == "succeeded"
    assert completed.state_version == 6
    events = restarted.read_events(
        tenant_id="tenant-1",
        run_id=run_id,
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
        "external_callback_received",
        "run_resume_claimed",
        "run_succeeded",
    ]


def test_durable_service_executes_claim_in_a_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    clock = _fixed_clock(2_000)
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "isolated-process.sqlite3",
            clock=clock,
        ),
        lease_owner_id="isolated-worker",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_target=ProcessWorkerTarget(module_name, "succeed"),
        worker_policy=ProcessWorkerPolicy(timeout_seconds=15),
        allow_unsafe_custom_worker_dev=True,
    )
    _admit_empty_run(service, run_id="run-isolated")

    completed = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-isolated",
    )

    result = canonical_loads(completed.terminal_result_json)
    assert isinstance(result, dict)
    outputs = result["outputs"]
    assert isinstance(outputs, dict)
    assert outputs["workerPid"] != os.getpid()


def test_durable_service_rejects_tampered_worker_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    clock = _fixed_clock(2_000)
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "tampered-authority.sqlite3",
            clock=clock,
        ),
        lease_owner_id="isolated-worker",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_target=ProcessWorkerTarget(
            module_name,
            "tamper_authority",
        ),
        worker_policy=ProcessWorkerPolicy(timeout_seconds=15),
        allow_unsafe_custom_worker_dev=True,
    )
    _admit_empty_run(service, run_id="run-tampered")

    with pytest.raises(
        ProcessWorkerProtocolError,
        match="authority digest",
    ):
        service.advance_run(
            tenant_id="tenant-1",
            run_id="run-tampered",
        )

    snapshot = service.get_run(
        tenant_id="tenant-1",
        run_id="run-tampered",
    )
    assert snapshot is not None
    assert snapshot.phase is AcceptedRunPhase.RUNNING
    assert snapshot.terminal_result_json is None


def test_durable_service_reaps_timed_out_worker_and_reclaims_after_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    database_path = tmp_path / "worker-timeout.sqlite3"
    timeout_clock = _fixed_clock(2_000)
    timed_out_service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            database_path,
            clock=timeout_clock,
        ),
        lease_owner_id="timed-out-worker",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=timeout_clock,
        worker_target=ProcessWorkerTarget(module_name, "spin_forever"),
        worker_policy=ProcessWorkerPolicy(
            timeout_seconds=0.3,
            termination_grace_seconds=0.2,
        ),
        allow_unsafe_custom_worker_dev=True,
    )
    _admit_empty_run(timed_out_service, run_id="run-timeout")

    with pytest.raises(ProcessWorkerDeadlineExceeded) as error:
        timed_out_service.advance_run(
            tenant_id="tenant-1",
            run_id="run-timeout",
        )

    assert all(child.pid != error.value.worker_pid for child in active_children())
    timed_out = timed_out_service.get_run(
        tenant_id="tenant-1",
        run_id="run-timeout",
    )
    assert timed_out is not None
    assert timed_out.phase is AcceptedRunPhase.RUNNING
    assert timed_out.claim is not None
    assert timed_out.claim.lease_generation == 1
    assert timed_out.terminal_result_json is None
    events = timed_out_service.read_events(
        tenant_id="tenant-1",
        run_id="run-timeout",
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
    ]

    recovery_clock = _fixed_clock(40_000)
    recovered_service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            database_path,
            clock=recovery_clock,
        ),
        lease_owner_id="recovery-worker",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=recovery_clock,
    )
    recovered = recovered_service.advance_run(
        tenant_id="tenant-1",
        run_id="run-timeout",
    )

    assert recovered.phase is AcceptedRunPhase.TERMINAL
    assert recovered.terminal_status == "succeeded"
    recovered_events = recovered_service.read_events(
        tenant_id="tenant-1",
        run_id="run-timeout",
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in recovered_events.events] == [
        "run_accepted",
        "run_claimed",
        "run_reclaimed",
        "run_succeeded",
    ]


def test_durable_service_preserves_existing_positional_constructor_order(
    tmp_path: Path,
) -> None:
    def clock() -> int:
        return 1_000

    registry = stdlib_registry()
    worker_policy = ProcessWorkerPolicy(timeout_seconds=15)

    service = DurableAcceptedRunService(
        SQLiteAcceptedRunRepository(
            tmp_path / "positional.sqlite3",
            clock=clock,
        ),
        "worker-1",
        30_000,
        registry,
        compile_graph_reference,
        clock,
        DEFAULT_DURABLE_WORKER_TARGET,
        worker_policy,
    )

    assert service.registry is registry
    assert service.compiler is compile_graph_reference
    assert service.clock is clock
    assert service.worker_target is DEFAULT_DURABLE_WORKER_TARGET
    assert service.worker_policy is worker_policy


def test_durable_service_rejects_custom_worker_without_unsafe_dev_opt_in(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="custom worker_target requires explicit",
    ):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "custom-worker-default-deny.sqlite3"
            ),
            lease_owner_id="worker-1",
            worker_target=ProcessWorkerTarget(
                "untrusted.worker",
                "publish_effect_then_spin",
            ),
        )


def test_durable_service_rejects_target_subclass_that_spoofs_default_equality(
    tmp_path: Path,
) -> None:
    class DefaultSpoofingTarget(ProcessWorkerTarget):
        def __eq__(self, other: object) -> bool:
            del other
            return True

        def __ne__(self, other: object) -> bool:
            del other
            return False

    with pytest.raises(
        ValueError,
        match="worker_target must be an exact ProcessWorkerTarget",
    ):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "custom-worker-subclass.sqlite3"
            ),
            lease_owner_id="worker-1",
            worker_target=DefaultSpoofingTarget(
                "untrusted.worker",
                "publish_effect_then_spin",
            ),
        )


def test_durable_service_rejects_non_boolean_custom_worker_dev_opt_in(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="allow_unsafe_custom_worker_dev must be a boolean",
    ):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "custom-worker-invalid-opt-in.sqlite3"
            ),
            lease_owner_id="worker-1",
            allow_unsafe_custom_worker_dev="yes",  # type: ignore[arg-type]
        )


def test_durable_service_cannot_enable_custom_worker_after_construction(
    tmp_path: Path,
) -> None:
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "custom-worker-late-opt-in.sqlite3"
        ),
        lease_owner_id="worker-1",
    )
    service.worker_target = ProcessWorkerTarget(
        "untrusted.worker",
        "publish_effect_then_spin",
    )
    service.allow_unsafe_custom_worker_dev = True

    with pytest.raises(
        ValueError,
        match="configuration changed after construction",
    ):
        service.advance_next_run()


def test_durable_service_cannot_disable_custom_worker_opt_in_after_construction(
    tmp_path: Path,
) -> None:
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "custom-worker-late-disable.sqlite3"
        ),
        lease_owner_id="worker-1",
        worker_target=ProcessWorkerTarget(
            "untrusted.worker",
            "publish_effect_then_spin",
        ),
        allow_unsafe_custom_worker_dev=True,
    )
    service.allow_unsafe_custom_worker_dev = False

    with pytest.raises(
        ValueError,
        match="configuration changed after construction",
    ):
        service.advance_next_run()


def test_durable_service_executes_validated_target_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    mutate_target = False
    service: DurableAcceptedRunService | None = None

    def clock() -> int:
        nonlocal mutate_target
        if mutate_target:
            assert service is not None
            service.worker_target = ProcessWorkerTarget(module_name, "succeed")
            mutate_target = False
        return 2_000

    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "custom-worker-validation-race.sqlite3",
            clock=clock,
        ),
        lease_owner_id="worker-1",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_policy=ProcessWorkerPolicy(timeout_seconds=15),
    )
    _admit_empty_run(service, run_id="run-validation-race")
    mutate_target = True

    completed = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-validation-race",
    )

    assert completed.terminal_result_json is not None
    result = canonical_loads(completed.terminal_result_json)
    assert isinstance(result, dict)
    assert result["status"] == "succeeded"
    assert result["outputs"] == {}


def test_default_durable_worker_rejects_a_replaced_registry(
    tmp_path: Path,
) -> None:
    registry = stdlib_registry()
    registry.replace(
        "prompt.render@1",
        lambda _inputs, _config, _context: {"text": "replacement"},
    )

    with pytest.raises(ValueError, match="custom registry"):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "custom-registry.sqlite3"
            ),
            lease_owner_id="worker-1",
            registry=registry,
            compiler=compile_graph_reference,
        )

    live_registry = stdlib_registry()
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(tmp_path / "mutated-registry.sqlite3"),
        lease_owner_id="worker-1",
        registry=live_registry,
        compiler=compile_graph_reference,
    )
    live_registry.replace(
        "prompt.render@1",
        lambda _inputs, _config, _context: {"text": "late replacement"},
    )

    with pytest.raises(ValueError, match="custom registry"):
        service.advance_next_run()


def test_default_durable_worker_rejects_direct_registry_mutation(
    tmp_path: Path,
) -> None:
    def replacement(_inputs, _config, _context):
        return {"text": "direct replacement"}

    registry = stdlib_registry()
    registry.blocks["prompt.render@1"] = replacement

    with pytest.raises(ValueError, match="custom registry"):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "direct-registry-mutation.sqlite3"
            ),
            lease_owner_id="worker-1",
            registry=registry,
            compiler=compile_graph_reference,
        )


def test_default_durable_worker_revalidates_registry_before_admission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "admission-registry-mutation.sqlite3"
    registry = stdlib_registry()
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(path),
        lease_owner_id="worker-1",
        registry=registry,
        compiler=compile_graph_reference,
    )
    registry.blocks["prompt.render@1"] = lambda _inputs, _config, _context: {
        "text": "late replacement"
    }

    with pytest.raises(ValueError, match="custom registry"):
        _admit_empty_run(service, run_id="poisoned-admission")

    assert (
        service.get_run(
            tenant_id="tenant-1",
            run_id="poisoned-admission",
        )
        is None
    )


def test_durable_service_rejects_mismatched_repository_clock(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="share the exact clock authority"):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(tmp_path / "clock-mismatch.sqlite3"),
            lease_owner_id="worker-1",
            clock=_fixed_clock(1_000),
        )


def test_durable_service_rechecks_clock_authority_before_admission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mutated-clock.sqlite3"
    clock = _fixed_clock(1_000)
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(path, clock=clock),
        lease_owner_id="worker-1",
        clock=clock,
    )
    service.clock = _fixed_clock(2_000)

    with pytest.raises(ValueError, match="share the exact clock authority"):
        _admit_empty_run(service, run_id="clock-split")

    assert (
        service.get_run(
            tenant_id="tenant-1",
            run_id="clock-split",
        )
        is None
    )


def test_default_durable_worker_rejects_a_custom_compiler(
    tmp_path: Path,
) -> None:
    def custom_compiler(document, block_catalog=None, **options):
        return compile_graph_reference(
            document,
            block_catalog=block_catalog,
            **options,
        )

    with pytest.raises(ValueError, match="custom compiler"):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "custom-compiler.sqlite3"
            ),
            lease_owner_id="worker-1",
            compiler=custom_compiler,
        )


def test_durable_worker_deadline_must_fit_inside_the_run_lease(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must fit inside the run lease"):
        DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                tmp_path / "worker-deadline.sqlite3"
            ),
            lease_owner_id="worker-1",
            lease_duration_ms=1_000,
            compiler=compile_graph_reference,
            worker_policy=ProcessWorkerPolicy(timeout_seconds=1),
        )


def test_durable_worker_deadline_shrinks_to_the_remaining_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    clock_values = iter((1_000, 30_600))

    def clock() -> int:
        return next(clock_values)

    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "remaining-lease.sqlite3",
            clock=clock,
        ),
        lease_owner_id="worker-1",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_target=ProcessWorkerTarget(module_name, "spin_forever"),
        worker_policy=ProcessWorkerPolicy(
            timeout_seconds=15,
            termination_grace_seconds=0.2,
        ),
        allow_unsafe_custom_worker_dev=True,
    )
    _admit_empty_run(service, run_id="run-remaining-lease")

    with pytest.raises(ProcessWorkerDeadlineExceeded) as error:
        service.advance_run(
            tenant_id="tenant-1",
            run_id="run-remaining-lease",
        )

    assert 0 < error.value.timeout_seconds < 0.11
    assert all(child.pid != error.value.worker_pid for child in active_children())


def test_oversized_durable_worker_request_fails_terminally(
    tmp_path: Path,
) -> None:
    clock = _fixed_clock(2_000)
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "oversized-worker-request.sqlite3",
            clock=clock,
        ),
        lease_owner_id="worker-1",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_policy=ProcessWorkerPolicy(
            timeout_seconds=15,
            max_request_bytes=256,
        ),
    )
    _admit_empty_run(service, run_id="run-oversized-request")

    failed = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-oversized-request",
    )
    replay = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-oversized-request",
    )

    assert failed.phase is AcceptedRunPhase.TERMINAL
    assert failed.terminal_status == "failed"
    assert replay == failed
    result = canonical_loads(failed.terminal_result_json)
    assert isinstance(result, dict)
    outputs = result["outputs"]
    assert isinstance(outputs, dict)
    error = outputs["error"]
    assert isinstance(error, dict)
    assert error["code"] == "worker_request_too_large"
    events = service.read_events(
        tenant_id="tenant-1",
        run_id="run-oversized-request",
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
        "run_failed",
    ]


def test_oversized_durable_worker_response_fails_terminally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_durable_worker_fixture(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    clock = _fixed_clock(2_000)
    service = DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(
            tmp_path / "oversized-worker-response.sqlite3",
            clock=clock,
        ),
        lease_owner_id="worker-1",
        lease_duration_ms=30_000,
        compiler=compile_graph_reference,
        clock=clock,
        worker_target=ProcessWorkerTarget(module_name, "succeed"),
        worker_policy=ProcessWorkerPolicy(
            timeout_seconds=15,
            max_result_bytes=256,
        ),
        allow_unsafe_custom_worker_dev=True,
    )
    _admit_empty_run(service, run_id="run-oversized-response")

    failed = service.advance_run(
        tenant_id="tenant-1",
        run_id="run-oversized-response",
    )

    assert failed.phase is AcceptedRunPhase.TERMINAL
    assert failed.terminal_status == "failed"
    result = canonical_loads(failed.terminal_result_json)
    assert isinstance(result, dict)
    outputs = result["outputs"]
    assert isinstance(outputs, dict)
    error = outputs["error"]
    assert isinstance(error, dict)
    assert error == {
        "code": "worker_response_too_large",
        "maxBytes": 256,
    }
