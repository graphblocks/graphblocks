from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from decimal import Decimal
import graphblocks
import pytest

from graphblocks.leases import InMemoryLeasePool
from graphblocks.plugins import BlockCatalog
from graphblocks.runtime import (
    ExecutionJournal,
    InProcessRuntime,
    JournalStateError,
    LocalRuntime,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    stdlib_registry,
)
from graphblocks.run_store import InMemoryRunStore, RunDeploymentProvenance, SQLiteRunStore


VALID_RESUME_TOKEN_HASH = "sha256:" + "a" * 64


def _accept_callback_receipt(
    _receipt,
    *,
    checkpoint,
    expected_checkpoint_digest,
    expected_release_digest,
) -> bool:
    return True


def test_runtime_waits_for_a_true_when_guard_dependency() -> None:
    calls: list[str] = []
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def branch(inputs, config, context):
        calls.append("branch")
        return {"value": "ran"}

    def condition(inputs, config, context):
        calls.append("condition")
        return {"enabled": True}

    registry.register("test.branch@1", branch)
    registry.register("test.condition@1", condition)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "true-when-guard"},
        "spec": {
            "interface": {"outputs": {"value": "graphblocks.ai/Text@1"}},
            "nodes": {
                "aBranch": {
                    "block": "test.branch@1",
                    "when": "zCondition.enabled",
                    "outputs": {"value": "$output.value"},
                },
                "zCondition": {"block": "test.condition@1"},
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "succeeded"
    assert result.outputs == {"value": "ran"}
    assert calls == ["condition", "branch"]


def test_runtime_skips_a_false_when_guard_without_invoking_the_block() -> None:
    calls: list[str] = []
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def branch(inputs, config, context):
        calls.append("branch")
        return {"value": "must-not-run"}

    def condition(inputs, config, context):
        calls.append("condition")
        return {"enabled": False}

    registry.register("test.branch@1", branch)
    registry.register("test.condition@1", condition)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "false-when-guard"},
        "spec": {
            "interface": {"outputs": {"enabled": "graphblocks.ai/Flag@1"}},
            "nodes": {
                "aBranch": {
                    "block": "test.branch@1",
                    "when": "zCondition.enabled",
                },
                "zCondition": {
                    "block": "test.condition@1",
                    "outputs": {"enabled": "$output.enabled"},
                },
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "succeeded"
    assert result.outputs == {"enabled": False}
    assert calls == ["condition"]
    skipped = [
        record
        for record in result.journal.records
        if record.kind == "node_succeeded" and record.payload["node"] == "aBranch"
    ]
    assert skipped[0].payload["skipped"] is True


def test_runtime_fails_closed_for_a_non_boolean_when_guard() -> None:
    calls: list[str] = []
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def branch(inputs, config, context):
        calls.append("branch")
        return {"value": "must-not-run"}

    def condition(inputs, config, context):
        calls.append("condition")
        return {"enabled": "true"}

    registry.register("test.branch@1", branch)
    registry.register("test.condition@1", condition)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-boolean-when-guard"},
        "spec": {
            "nodes": {
                "aBranch": {
                    "block": "test.branch@1",
                    "when": "zCondition.enabled",
                },
                "zCondition": {"block": "test.condition@1"},
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert calls == ["condition"]
    failure = next(
        record for record in result.journal.records if record.kind == "node_failed"
    )
    assert failure.payload["node"] == "aBranch"
    assert "boolean" in failure.payload["error"]


def test_runtime_converts_output_projection_errors_to_terminal_failure() -> None:
    pool = InMemoryLeasePool({"model": 1})
    store = InMemoryRunStore()
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def produce(inputs, config, context):
        context["lease_pool"].acquire("model", owner=context["run_id"])
        return {"value": "ok"}

    registry.register("test.produce@1", produce)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "projection-failure"},
        "spec": {
            "interface": {"outputs": {"value": "graphblocks.ai/Text@1"}},
            "nodes": {"produce": {"block": "test.produce@1"}},
            "edges": [
                {"from": "produce.missing", "to": "$output.value"}
            ],
        },
    }

    result = InProcessRuntime(
        registry,
        run_store=store,
        lease_pool=pool,
    ).run(graph, {}, run_id="run-projection-failure")

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert store.get_run(result.run_id).status == "failed"
    assert pool.available("model") == 1
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "produce"
    assert "missing" in failed[0].payload["error"]


def test_runtime_converts_checkpoint_serialization_errors_to_terminal_failure() -> None:
    pool = InMemoryLeasePool({"model": 1})
    store = InMemoryRunStore()
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def wait(inputs, config, context):
        context["lease_pool"].acquire("model", owner=context["run_id"])
        return {
            "wait": {
                "state": "waiting_callback",
                "checkpoint": True,
                "operation": {"not_json": object()},
            }
        }

    registry.register("async.await_callback@1", wait)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "checkpoint-serialization-failure"},
        "spec": {
            "nodes": {
                "wait": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "30m",
                        "idempotencyKey": "checkpoint-failure-idem",
                        "callback": {"schema": "schemas/Callback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            }
        },
    }

    result = InProcessRuntime(
        registry,
        run_store=store,
        lease_pool=pool,
    ).run(graph, {}, run_id="run-checkpoint-failure")

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert store.get_run(result.run_id).status == "failed"
    assert pool.available("model") == 1
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "wait"
    assert "JSON serializable" in failed[0].payload["error"]


def test_runtime_converts_mixed_output_key_errors_to_terminal_failure() -> None:
    pool = InMemoryLeasePool({"model": 1})
    store = InMemoryRunStore()
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)

    def produce(inputs, config, context):
        context["lease_pool"].acquire("model", owner=context["run_id"])
        return {"value": "ok", 2: "invalid-key"}

    registry.register("test.produce@1", produce)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "mixed-output-key-failure"},
        "spec": {"nodes": {"produce": {"block": "test.produce@1"}}},
    }

    result = InProcessRuntime(
        registry,
        run_store=store,
        lease_pool=pool,
    ).run(graph, {}, run_id="run-mixed-output-key-failure")

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert store.get_run(result.run_id).status == "failed"
    assert pool.available("model") == 1
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "produce"
    assert "not supported" in failed[0].payload["error"]


def test_runtime_terminalizes_callback_resume_projection_errors() -> None:
    pool = InMemoryLeasePool({"callback": 1})
    store = InMemoryRunStore()
    registry = RuntimeRegistry(block_catalog=BlockCatalog({}), allow_untyped=True)
    run_id = "run-resume-projection-failure"
    operation = {
        "operation_id": "operation-resume-projection-failure",
        "run_id": run_id,
        "node_id": "wait",
        "attempt_id": "attempt-1",
        "kind": "ci_job",
        "state": "waiting_callback",
        "provider_operation_id": "provider-operation-1",
        "resume_token_hash": VALID_RESUME_TOKEN_HASH,
        "idempotency_key": "idem-resume-projection-failure",
        "expected_schema": "schemas/CICallback@1",
        "created_at_unix_ms": 1_000,
        "submitted_at_unix_ms": 1_050,
        "expires_at_unix_ms": 10_000,
    }

    def wait(inputs, config, context):
        context["lease_pool"].acquire("callback", owner=context["run_id"])
        return {
            "wait": {
                "state": "waiting_callback",
                "checkpoint": True,
                "operation": operation,
            }
        }

    registry.register("async.await_callback@1", wait)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "resume-projection-failure"},
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/Any@1"}},
            "nodes": {
                "wait": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "30m",
                        "idempotencyKey": "idem-resume-projection-failure",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                }
            },
            "edges": [{"from": "wait.missing", "to": "$output.result"}],
        },
    }
    journals: dict[str, ExecutionJournal] = {}
    runtime = InProcessRuntime(
        registry,
        run_store=store,
        lease_pool=pool,
        callback_receipt_verifier=_accept_callback_receipt,
        journal_factory=lambda journal_run_id: journals.setdefault(
            journal_run_id,
            ExecutionJournal(journal_run_id),
        ),
    )

    waiting = runtime.run(graph, {}, run_id=run_id)
    assert waiting.checkpoint is not None
    assert pool.available("callback") == 0
    callback_payload = {"status": "completed"}
    receipt = {
        "operation_id": operation["operation_id"],
        "run_id": run_id,
        "node_id": "wait",
        "attempt_id": operation["attempt_id"],
        "provider_operation_id": operation["provider_operation_id"],
        "operation_idempotency_key": operation["idempotency_key"],
        "callback_idempotency_key": "delivery-resume-projection-failure",
        "resume_token_hash": operation["resume_token_hash"],
        "schema_id": operation["expected_schema"],
        "schema_validated": True,
        "payload": callback_payload,
        "payload_digest": graphblocks.canonical_hash(callback_payload),
        "received_at_unix_ms": 2_000,
        "verified_by": "callback-relay",
        "resume_admission": {
            "policy_reevaluated": True,
            "budget_reserved": True,
            "release_compatible": True,
            "ownership_fenced": True,
        },
    }

    result = runtime.run(
        graph,
        {},
        run_id=run_id,
        checkpoint=waiting.checkpoint,
        callback_receipt=receipt,
    )

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert store.get_run(run_id).status == "failed"
    assert pool.available("callback") == 1
    failure = next(
        record for record in result.journal.records if record.kind == "node_failed"
    )
    assert failure.payload["node"] == "wait"
    assert "missing" in failure.payload["error"]
    with pytest.raises(
        ValueError,
        match="runtime checkpoint state does not match the issuing runtime",
    ):
        runtime.run(
            graph,
            {},
            run_id=run_id,
            checkpoint=waiting.checkpoint,
            callback_receipt=receipt,
        )


def test_runtime_executes_conversation_vertical_slice() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "chat-vertical-slice"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Message@1"},
                "outputs": {"answer": "graphblocks.ai/Answer@1"},
            },
            "nodes": {
                "begin": {"block": "conversation.begin_turn@1"},
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Answer: {message.text}"},
                    "inputs": {"message": "$input.message"},
                },
                "generate": {
                    "block": "model.generate@1",
                    "config": {"script": {"Answer: Hello": "Hello from the scripted model."}},
                    "inputs": {"prompt": "render.prompt"},
                },
                "commit": {
                    "block": "conversation.commit_turn@1",
                    "inputs": {
                        "transaction": "begin.transaction",
                        "candidate": "generate.response",
                    },
                    "outputs": {"answer": "$output.answer"},
                },
            },
        },
    }
    runtime = InProcessRuntime(stdlib_registry())

    result = runtime.run(graph, {"message": {"text": "Hello"}})

    assert result.status == "succeeded"
    assert result.outputs == {
        "answer": {
            "conversationId": "conversation-default",
            "text": "Hello from the scripted model.",
            "turnId": "turn-000001",
        }
    }
    assert [record.kind for record in result.journal.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]


def test_runtime_suspends_at_callback_wait_and_resumes_from_checkpoint() -> None:
    prepare_calls = 0
    consume_calls = 0

    def prepare(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        nonlocal prepare_calls
        prepare_calls += 1
        return {"subject": {"change": "patch-1"}}

    def consume(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        nonlocal consume_calls
        consume_calls += 1
        assert store.get_run("run-runtime-resume-1").status == "resuming"
        return {"result": inputs["callback"]}

    registry = stdlib_registry(allow_untyped=True)
    registry.register("test.prepare@1", prepare)
    registry.register("test.consume-callback@1", consume)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-callback-checkpoint"},
        "spec": {
            "nodes": {
                "prepare": {"block": "test.prepare@1"},
                "start": {
                    "block": "async.start_operation@1",
                    "inputs": {"subject": "prepare.subject"},
                    "config": {
                        "operationId": "operation-runtime-resume-1",
                        "runId": "run-runtime-resume-1",
                        "nodeId": "wait",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-operation-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-operation-runtime-resume-1",
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
                        "idempotencyKey": "idem-operation-runtime-resume-1",
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
                "consume": {
                    "block": "test.consume-callback@1",
                    "inputs": {"callback": "wait.callback"},
                    "outputs": {"result": "$output.result"},
                },
            }
        },
    }
    store = InMemoryRunStore()
    journals: dict[str, ExecutionJournal] = {}
    runtime = InProcessRuntime(
        registry,
        run_store=store,
        callback_receipt_verifier=_accept_callback_receipt,
        journal_factory=lambda run_id: journals.setdefault(
            run_id,
            ExecutionJournal(run_id),
        ),
    )

    waiting = runtime.run(graph, {}, run_id="run-runtime-resume-1")

    assert waiting.status == "waiting_callback"
    assert waiting.checkpoint is not None
    assert graphblocks.RuntimeCheckpoint is type(waiting.checkpoint)
    assert waiting.checkpoint.wait_node == "wait"
    assert waiting.checkpoint.operation["operation_id"] == "operation-runtime-resume-1"
    assert waiting.journal.records[-1].kind == "run_waiting_callback"
    assert store.get_run("run-runtime-resume-1").status == "waiting_callback"
    assert prepare_calls == 1
    assert consume_calls == 0

    valid_checkpoint_operation = dict(waiting.checkpoint.operation)
    invalid_checkpoint_operations = (
        (
            {**valid_checkpoint_operation, "kind": "not-a-real-kind"},
            "runtime checkpoint operation kind must be a valid async operation kind",
        ),
        (
            {**valid_checkpoint_operation, "node_id": "foreign-node"},
            "runtime checkpoint operation node_id must belong to checkpoint graph state",
        ),
        (
            {
                **valid_checkpoint_operation,
                "resume_token_hash": "sha256:" + "z" * 64,
            },
            "runtime checkpoint operation resume_token_hash must be a canonical sha256 digest",
        ),
        (
            {**valid_checkpoint_operation, "submitted_at_unix_ms": "1050"},
            "runtime checkpoint operation submitted_at_unix_ms must be an unsigned 64-bit integer",
        ),
        (
            {**valid_checkpoint_operation, "expires_at_unix_ms": "61000"},
            "runtime checkpoint operation expires_at_unix_ms must be an unsigned 64-bit integer",
        ),
        (
            {**valid_checkpoint_operation, "submitted_at_unix_ms": 999},
            "runtime checkpoint operation submitted_at_unix_ms must not precede created_at_unix_ms",
        ),
        (
            {**valid_checkpoint_operation, "expires_at_unix_ms": 1_050},
            "runtime checkpoint operation expires_at_unix_ms must be after submitted_at_unix_ms",
        ),
        (
            {**valid_checkpoint_operation, "completed_at_unix_ms": 2_000},
            "runtime checkpoint waiting operation must not have completed_at_unix_ms",
        ),
        (
            {
                **valid_checkpoint_operation,
                "expires_at_unix_ms": None,
                "infinite_wait_policy": None,
            },
            "runtime checkpoint waiting operation requires expires_at_unix_ms or infinite_wait_policy",
        ),
        (
            {
                **valid_checkpoint_operation,
                "infinite_wait_policy": "operator_review_required",
            },
            "runtime checkpoint waiting operation must not define both expires_at_unix_ms and infinite_wait_policy",
        ),
        (
            {
                **valid_checkpoint_operation,
                "expires_at_unix_ms": None,
                "infinite_wait_policy": " operator_review_required ",
            },
            "runtime checkpoint operation infinite_wait_policy must be an exact non-empty string",
        ),
    )
    for invalid_operation, expected_error in invalid_checkpoint_operations:
        with pytest.raises(ValueError, match=expected_error):
            replace(waiting.checkpoint, operation=invalid_operation)

    callback_receipt = {
        "operation_id": "operation-runtime-resume-1",
        "run_id": "run-runtime-resume-1",
        "node_id": "wait",
        "attempt_id": "attempt-1",
        "provider_operation_id": "provider-operation-1",
        "operation_idempotency_key": "idem-operation-runtime-resume-1",
        "callback_idempotency_key": "delivery-runtime-resume-1",
        "resume_token_hash": VALID_RESUME_TOKEN_HASH,
        "schema_id": "schemas/CICallback@1",
        "schema_validated": True,
        "payload": {"status": "completed", "conclusion": "success"},
        "payload_digest": graphblocks.canonical_hash(
            {"status": "completed", "conclusion": "success"}
        ),
        "received_at_unix_ms": 2_000,
        "verified_by": "callback-relay",
        "resume_admission": {
            "policy_reevaluated": True,
            "budget_reserved": True,
            "release_compatible": True,
            "ownership_fenced": True,
        },
    }
    with pytest.raises(
        ValueError,
        match="runtime callback_receipt must be before operation expiration",
    ):
        runtime.run(
            graph,
            {},
            run_id="run-runtime-resume-1",
            checkpoint=waiting.checkpoint,
            callback_receipt={
                **callback_receipt,
                "received_at_unix_ms": waiting.checkpoint.operation[
                    "expires_at_unix_ms"
                ],
            },
        )
    with pytest.raises(
        ValueError,
        match="runtime callback_receipt verified_by must identify an authenticated principal",
    ):
        runtime.run(
            graph,
            {},
            run_id="run-runtime-resume-1",
            checkpoint=waiting.checkpoint,
            callback_receipt={**callback_receipt, "verified_by": "unauthenticated"},
        )
    with pytest.raises(
        ValueError,
        match="runtime callback_receipt verified_by must identify an authenticated principal",
    ):
        runtime.run(
            graph,
            {},
            run_id="run-runtime-resume-1",
            checkpoint=waiting.checkpoint,
            callback_receipt={**callback_receipt, "verified_by": " unauthenticated "},
        )
    with pytest.raises(
        ValueError,
        match="runtime checkpoint inputs must match original run inputs",
    ):
        runtime.run(
            graph,
            {"changed": True},
            run_id="run-runtime-resume-1",
            checkpoint=waiting.checkpoint,
            callback_receipt=callback_receipt,
        )
    with pytest.raises(
        ValueError,
        match="runtime checkpoint state does not match the issuing runtime",
    ):
        runtime.run(
            graph,
            {},
            run_id="run-runtime-resume-1",
            checkpoint=replace(
                waiting.checkpoint,
                remaining_nodes=("wait",),
            ),
            callback_receipt=callback_receipt,
        )

    resumed = runtime.run(
        graph,
        {},
        run_id="run-runtime-resume-1",
        checkpoint=waiting.checkpoint,
        callback_receipt=callback_receipt,
    )

    assert resumed.status == "succeeded"
    assert resumed.outputs == {
        "result": {"status": "completed", "conclusion": "success"}
    }
    assert resumed.checkpoint is None
    resumed_journal_kinds = [record.kind for record in resumed.journal.records]
    assert resumed_journal_kinds.index("run_waiting_callback") < (
        resumed_journal_kinds.index("external_callback_received")
    )
    assert resumed_journal_kinds.index("external_callback_received") < (
        resumed_journal_kinds.index("run_resuming")
    )
    assert resumed_journal_kinds.index("run_resuming") < (
        resumed_journal_kinds.index("node_started", resumed_journal_kinds.index("run_resuming"))
    )
    callback_record = next(
        record
        for record in resumed.journal.records
        if record.kind == "external_callback_received"
    )
    assert callback_record.payload["callbackIdempotencyKey"] == (
        "delivery-runtime-resume-1"
    )
    assert store.get_run("run-runtime-resume-1").status == "succeeded"
    assert prepare_calls == 1
    assert consume_calls == 1

    cancelled_graph = deepcopy(graph)
    del cancelled_graph["spec"]["nodes"]["consume"]
    cancelled_store = InMemoryRunStore()
    cancelled_token = graphblocks.CancellationToken()
    cancelled_runtime = InProcessRuntime(
        registry,
        run_store=cancelled_store,
        cancellation_token=cancelled_token,
        callback_receipt_verifier=_accept_callback_receipt,
    )
    cancelled_wait = cancelled_runtime.run(
        cancelled_graph,
        {},
        run_id="run-runtime-resume-1",
    )
    assert cancelled_wait.checkpoint is not None
    cancelled_token.cancel("operator stop")

    cancelled = cancelled_runtime.run(
        cancelled_graph,
        {},
        run_id="run-runtime-resume-1",
        checkpoint=cancelled_wait.checkpoint,
        callback_receipt=callback_receipt,
    )

    assert cancelled.status == "cancelled"
    assert cancelled_store.get_run("run-runtime-resume-1").status == "cancelled"

    duplicate_id_runtime = InProcessRuntime(registry)
    first_wait = duplicate_id_runtime.run(
        graph,
        {},
        run_id="run-runtime-resume-1",
    )
    second_wait = duplicate_id_runtime.run(
        graph,
        {},
        run_id="run-runtime-resume-1",
    )
    assert first_wait.checkpoint is not None
    assert second_wait.checkpoint is not None
    assert first_wait.checkpoint.checkpoint_id != second_wait.checkpoint.checkpoint_id

    malformed_registry = stdlib_registry(allow_untyped=True)
    malformed_registry.register("test.prepare@1", prepare)
    malformed_registry.register("test.consume-callback@1", consume)
    malformed_registry.replace(
        "async.await_callback@1",
        lambda inputs, config, context: {
            "wait": {
                "state": "waiting_callback",
                "checkpoint": True,
                "operation": {"run_id": "run-runtime-resume-1"},
            }
        },
    )
    malformed = InProcessRuntime(malformed_registry).run(
        graph,
        {},
        run_id="run-runtime-resume-1",
    )

    assert malformed.status == "failed"
    assert malformed.journal.terminal_kind == "run_failed"
    malformed_failure = next(
        record for record in malformed.journal.records if record.kind == "node_failed"
    )
    assert (
        "runtime checkpoint operation operation_id must be an exact non-empty string"
        in malformed_failure.payload["error"]
    )


def test_runtime_requires_conditionally_required_resumed_callback_output() -> None:
    registry = stdlib_registry()
    conditional_descriptor = BlockCatalog.from_blocks(
        [
            {
                "typeId": "async.await_callback",
                "version": 1,
                "inputs": [
                    {
                        "name": "operation",
                        "type": "graphblocks.ai/AsyncOperation@1",
                    }
                ],
                "outputs": [
                    {"name": "wait", "type": "graphblocks.ai/AsyncWait@1"},
                    {
                        "name": "callback",
                        "type": "Any",
                        "required": False,
                        "requiredWhen": {"phase": "resumed"},
                    },
                    {
                        "name": "operation",
                        "type": "graphblocks.ai/AsyncOperation@1",
                        "required": False,
                        "requiredWhen": {"phase": "resumed"},
                    },
                    {
                        "name": "resumeEvidence",
                        "type": "Any",
                        "required": False,
                        "requiredWhen": {"phase": "resumed"},
                    },
                ],
            }
        ]
    ).get("async.await_callback@1")
    assert conditional_descriptor is not None
    registry.block_catalog = BlockCatalog(
        {
            **registry.block_catalog.descriptors,
            "async.await_callback@1": conditional_descriptor,
        }
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-resumed-conditional-output"},
        "spec": {
            "nodes": {
                "start": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "operation-resumed-conditional-output",
                        "runId": "run-resumed-conditional-output",
                        "nodeId": "wait",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-operation-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-resumed-conditional-output",
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
                        "idempotencyKey": "idem-resumed-conditional-output",
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
    runtime = InProcessRuntime(
        registry,
        callback_receipt_verifier=_accept_callback_receipt,
    )

    waiting = runtime.run(graph, {}, run_id="run-resumed-conditional-output")

    assert waiting.status == "waiting_callback"
    assert waiting.checkpoint is not None
    operation = waiting.checkpoint.operation
    payload = {"status": "completed"}
    resumed = runtime.run(
        graph,
        {},
        run_id="run-resumed-conditional-output",
        checkpoint=waiting.checkpoint,
        callback_receipt={
            "operation_id": operation["operation_id"],
            "run_id": operation["run_id"],
            "node_id": operation["node_id"],
            "attempt_id": operation["attempt_id"],
            "provider_operation_id": operation["provider_operation_id"],
            "operation_idempotency_key": operation["idempotency_key"],
            "callback_idempotency_key": "delivery-resumed-conditional-output",
            "resume_token_hash": operation["resume_token_hash"],
            "schema_id": operation["expected_schema"],
            "schema_validated": True,
            "payload": payload,
            "payload_digest": graphblocks.canonical_hash(payload),
            "received_at_unix_ms": 2_000,
            "verified_by": "callback-relay",
            "resume_admission": {
                "policy_reevaluated": True,
                "budget_reserved": True,
                "release_compatible": True,
                "ownership_fenced": True,
            },
        },
    )

    assert resumed.status == "failed"
    failure = next(
        record for record in resumed.journal.records if record.kind == "node_failed"
    )
    assert failure.payload["error"] == (
        "async.await_callback@1 omitted required output(s): resumeEvidence"
    )


def test_stdlib_policy_stop_turn_blocks_late_commit() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "policy-stopped-turn"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Message@1"},
                "outputs": {"answer": "graphblocks.ai/Answer@1"},
            },
            "nodes": {
                "begin": {"block": "conversation.begin_turn@1"},
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Answer: {message.text}"},
                    "inputs": {"message": "$input.message"},
                },
                "generate": {
                    "block": "model.generate@1",
                    "config": {"script": {"Answer: Hello": "blocked answer"}},
                    "inputs": {"prompt": "render.prompt"},
                },
                "stop": {
                    "block": "conversation.policy_stop_turn@1",
                    "inputs": {"transaction": "begin.transaction"},
                },
                "commit": {
                    "block": "conversation.commit_turn@1",
                    "inputs": {
                        "transaction": "stop.transaction",
                        "candidate": "generate.response",
                    },
                    "outputs": {"answer": "$output.answer"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"message": {"text": "Hello"}})

    assert result.status == "failed"
    assert result.outputs == {}
    assert result.journal.terminal_kind == "run_failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "commit"
    assert failed[0].payload["error"] == "conversation.commit_turn@1 cannot commit policy-stopped turn"


def test_stdlib_runtime_executes_tool_resolution_and_agent_run() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "agent-turn"},
        "spec": {
            "interface": {
                "inputs": {"messages": "graphblocks.ai/Messages@1"},
                "outputs": {
                    "candidate": "graphblocks.ai/TurnCandidate@1",
                    "tools": "graphblocks.ai/ResolvedTools@1",
                },
            },
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1",
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-search",
                                "toolName": "knowledge.search",
                                "implementation": {
                                    "kind": "block",
                                    "block": "knowledge.search@1",
                                    "input_mapping": {"query": "$arguments.query"},
                                    "output_mapping": {"items": "$result.items"},
                                },
                                "effects": ["external_read"],
                                "approval": "never",
                                "timeoutMs": 250,
                            }
                        ],
                        "scope": {"principalTools": ["knowledge.search"]},
                    },
                    "outputs": {"tools": "$output.tools"},
                },
                "agent": {
                    "block": "agent.run@1",
                    "config": {
                        "response": "Hello from the agent.",
                        "outputPolicy": {"profileRef": "assistant-output-standard"},
                    },
                    "inputs": {
                        "messages": "$input.messages",
                        "tools": "resolve.tools",
                    },
                    "outputs": {"candidate": "$output.candidate"},
                },
            },
        },
    }

    store = InMemoryRunStore()
    result = InProcessRuntime(stdlib_registry(), run_store=store).run(
        graph,
        {"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert result.status == "succeeded"
    candidate = result.outputs["candidate"]
    resolved_tool = result.outputs["tools"][0]
    assert candidate["text"] == "Hello from the agent."
    assert candidate["finishReason"] == "scripted"
    assert candidate["toolCount"] == 1
    assert candidate["outputPolicyProfileRef"] == "assistant-output-standard"
    assert candidate["modelVisibleTools"] == [
        {
            "toolName": "knowledge.search",
            "resolvedToolId": resolved_tool["resolved_tool_id"],
            "definitionDigest": resolved_tool["definition_digest"],
            "bindingDigest": resolved_tool["binding_digest"],
            "effectivePolicySnapshotId": resolved_tool["effective_policy_snapshot_id"],
            "allowedForPrincipal": True,
            "validUntil": None,
        }
    ]
    assert resolved_tool["binding"]["implementation"]["input_mapping"] == {"query": "$arguments.query"}
    assert resolved_tool["binding"]["implementation"]["output_mapping"] == {"items": "$result.items"}
    assert result.outputs["tools"][0]["definition"]["name"] == "knowledge.search"
    assert result.outputs["tools"][0]["allowed_for_principal"] is True
    assert result.outputs["tools"][0]["binding"]["timeout_ms"] == 250
    stored = store.get_run(result.run_id)
    assert [tool.tool_name for tool in stored.model_visible_tools] == ["knowledge.search"]
    assert stored.model_visible_tools[0].resolved_tool_id == resolved_tool["resolved_tool_id"]
    assert stored.model_visible_tools[0].definition_digest == resolved_tool["definition_digest"]
    assert stored.model_visible_tools[0].binding_digest == resolved_tool["binding_digest"]
    assert stored.model_visible_tools[0].effective_policy_snapshot_id == "policy-snapshot-1"
    assert stored.model_visible_tools[0].allowed_for_principal is True


def test_stdlib_agent_run_rejects_unresolved_tool_entries() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "agent-rejects-unresolved-tool"},
        "spec": {
            "nodes": {
                "agent": {
                    "block": "agent.run@1",
                    "config": {"response": "should not run"},
                    "inputs": {
                        "messages": "$input.messages",
                        "tools": "$input.tools",
                    },
                    "outputs": {"candidate": "$output.candidate"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"definition": {"name": "knowledge.search"}}],
        },
    )

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert "agent.run@1 input 'tools[0].resolved_tool_id' must be a string" in failed[0].payload["error"]


@pytest.mark.parametrize("timeout_ms", [True, "250", -1])
def test_stdlib_tool_resolution_rejects_non_integer_timeout_ms(timeout_ms: object) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tool-timeout-ms-validation"},
        "spec": {
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1",
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-search",
                                "toolName": "knowledge.search",
                                "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                "effects": ["external_read"],
                                "approval": "never",
                                "timeoutMs": timeout_ms,
                            }
                        ],
                        "scope": {"principalTools": ["knowledge.search"]},
                    },
                    "outputs": {"tools": "$output.tools"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "resolve"
    assert "tools.resolve@1 config.bindings[0].timeoutMs must be a non-negative integer" in failed[0].payload["error"]


@pytest.mark.parametrize(
    "principal_tools,error",
    [
        (["knowledge.search", 1], "tools.resolve@1 scope principalTools entries must be strings"),
        (["knowledge.search", " "], "tools.resolve@1 scope principalTools entries must not be empty"),
    ],
)
def test_stdlib_tool_resolution_rejects_invalid_scope_entries(
    principal_tools: list[object],
    error: str,
) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tool-scope-entry-validation"},
        "spec": {
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1",
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-search",
                                "toolName": "knowledge.search",
                                "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                "effects": ["external_read"],
                                "approval": "never",
                            }
                        ],
                        "scope": {"principalTools": principal_tools},
                    },
                    "outputs": {"tools": "$output.tools"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "resolve"
    assert error in failed[0].payload["error"]


@pytest.mark.parametrize(
    "path,value,error",
    [
        (
            ["definitions", 0, "name"],
            42,
            "tools.resolve@1 config.definitions[0].name must be a string",
        ),
        (
            ["definitions", 0, "inputSchema"],
            " ",
            "tools.resolve@1 config.definitions[0].inputSchema must not be empty",
        ),
        (
            ["definitions", 0, "tags"],
            ["search", 1],
            "tools.resolve@1 config.definitions[0].tags entries must be strings",
        ),
        (
            ["bindings", 0, "bindingId"],
            42,
            "tools.resolve@1 config.bindings[0].bindingId must be a string",
        ),
        (
            ["bindings", 0, "toolName"],
            "",
            "tools.resolve@1 config.bindings[0].toolName must not be empty",
        ),
        (
            ["bindings", 0, "effects"],
            ["external_read", 1],
            "tools.resolve@1 config.bindings[0].effects entries must be strings",
        ),
        (
            ["bindings", 0, "approval"],
            1,
            "tools.resolve@1 config.bindings[0].approval must be a string",
        ),
        (
            ["bindings", 0, "retryPolicyRef"],
            " ",
            "tools.resolve@1 config.bindings[0].retryPolicyRef must not be empty",
        ),
        (
            ["bindings", 0, "implementation", "kind"],
            1,
            "tools.resolve@1 config.bindings[0].implementation.kind must be a string",
        ),
        (
            ["bindings", 0, "implementation", "block"],
            1,
            "tools.resolve@1 config.bindings[0].implementation.block must be a string",
        ),
        (
            ["bindings", 0, "implementation", "inputMapping"],
            {"query": 1},
            "tools.resolve@1 config.bindings[0].implementation.inputMapping entries must be strings",
        ),
    ],
)
def test_stdlib_tool_resolution_rejects_invalid_definition_and_binding_fields(
    path: list[object],
    value: object,
    error: str,
) -> None:
    tool_config: dict[str, object] = {
        "effectivePolicySnapshotId": "policy-snapshot-1",
        "definitions": [
            {
                "name": "knowledge.search",
                "description": "Search support documentation.",
                "inputSchema": "schemas/SearchRequest@1",
            }
        ],
        "bindings": [
            {
                "bindingId": "binding-search",
                "toolName": "knowledge.search",
                "implementation": {"kind": "block", "block": "knowledge.search@1"},
                "effects": ["external_read"],
                "approval": "never",
            }
        ],
        "scope": {"principalTools": ["knowledge.search"]},
    }
    cursor: object = tool_config
    for part in path[:-1]:
        if isinstance(cursor, dict) and isinstance(part, str):
            cursor = cursor[part]
        elif isinstance(cursor, list) and isinstance(part, int):
            cursor = cursor[part]
        else:
            raise AssertionError(f"invalid test mutation path: {path}")
    if not isinstance(cursor, dict) or not isinstance(path[-1], str):
        raise AssertionError(f"invalid test mutation target: {path}")
    cursor[path[-1]] = value
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tool-field-validation"},
        "spec": {
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": tool_config,
                    "outputs": {"tools": "$output.tools"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "resolve"
    assert error in failed[0].payload["error"]


def test_journal_rejects_second_terminal_record() -> None:
    journal = ExecutionJournal("run-test")

    journal.append_terminal("run_succeeded", {"outputs": {}})

    with pytest.raises(JournalStateError):
        journal.append_terminal("run_failed", {"error": "late"})


def test_journal_rejects_output_after_terminal() -> None:
    journal = ExecutionJournal("run-test")

    journal.append_terminal("run_succeeded", {"outputs": {}})

    with pytest.raises(JournalStateError):
        journal.append("node_succeeded", {"node": "late"})


def test_runtime_fails_when_block_is_not_registered() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-block"},
        "spec": {
            "nodes": {"missing": {"block": "missing.block@1"}},
            "edges": [{"from": "missing.value", "to": "$output.value"}],
        },
    }

    with pytest.raises(ValueError, match="GB1022.*not declared in the block catalog"):
        InProcessRuntime(RuntimeRegistry()).run(graph, {})


@pytest.mark.parametrize(
    "invalid_value",
    (
        object(),
        float("nan"),
        {1: "non-string-key"},
    ),
)
def test_runtime_rejects_non_json_block_outputs(invalid_value: object) -> None:
    registry = RuntimeRegistry(allow_untyped=True)

    def invalid_block(inputs, config, context):
        return {"value": invalid_value}

    registry.register("test.invalid-output@1", invalid_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invalid-block-output"},
        "spec": {
            "nodes": {
                "invalid": {
                    "block": "test.invalid-output@1",
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    for index, runtime in enumerate(
        (InProcessRuntime(registry), LocalRuntime(registry)),
        start=1,
    ):
        result = runtime.run(graph, {}, run_id=f"invalid-output-{index}")

        assert result.status == "failed"
        assert result.outputs == {}
        failure = next(
            record for record in result.journal.records if record.kind == "node_failed"
        )
        assert failure.payload["error"] == (
            "test.invalid-output@1 output must be valid strict JSON"
        )


def test_runtime_does_not_coerce_non_numeric_retry_attempts() -> None:
    attempts = {"count": 0}
    registry = RuntimeRegistry(allow_untyped=True)

    def flaky_block(inputs, config, context):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"value": "ok"}

    registry.register("test.flaky@1", flaky_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-numeric-retry-runtime"},
        "spec": {
            "nodes": {
                "flaky": {
                    "block": "test.flaky@1",
                    "flow": {"retry": {"maxAttempts": "2"}},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert attempts["count"] == 1
    assert result.status == "failed"
    assert result.outputs == {}
    assert result.journal.terminal_kind == "run_failed"
    assert "node_retry" not in [record.kind for record in result.journal.records]


def test_runtime_ignores_malformed_retry_attempts_without_crashing() -> None:
    registry = RuntimeRegistry(allow_untyped=True)

    def failing_block(inputs, config, context):
        raise RuntimeError("failed once")

    registry.register("test.fail@1", failing_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-retry-runtime"},
        "spec": {
            "nodes": {
                "fail": {
                    "block": "test.fail@1",
                    "flow": {"retry": {"maxAttempts": "two"}},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert [record.kind for record in result.journal.records] == [
        "run_started",
        "node_started",
        "node_failed",
        "run_failed",
    ]


@pytest.mark.parametrize("idempotency_key", (None, "", " ", " idem-1", {"path": "$input.request_id"}))
def test_runtime_does_not_retry_state_changes_with_invalid_idempotency_key(
    idempotency_key: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    registry = RuntimeRegistry(allow_untyped=True)

    def failing_write(inputs, config, context):
        attempts["count"] += 1
        raise RuntimeError("write failed")

    registry.register("test.write@1", failing_write)
    retry = {"maxAttempts": 2}
    if idempotency_key is not None:
        retry["idempotencyKey"] = idempotency_key
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unsafe-invalid-idempotency-key-runtime"},
        "spec": {
            "nodes": {
                "write": {
                    "block": "test.write@1",
                    "effects": ["external_write"],
                    "flow": {"retry": retry},
                }
            }
        },
    }
    monkeypatch.setattr(
        "graphblocks.runtime.compile_graph",
        lambda document, **_kwargs: graphblocks.Plan(
            document,
            "sha256:test",
            graphblocks.DiagnosticSet(()),
        ),
    )

    result = InProcessRuntime(registry).run(graph, {})

    assert attempts["count"] == 1
    assert result.status == "failed"
    assert "node_retry" not in [record.kind for record in result.journal.records]


def test_runtime_updates_supplied_run_store_status() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "stored-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Stored {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    store = InMemoryRunStore()

    result = InProcessRuntime(stdlib_registry(), run_store=store).run(graph, {"message": {"text": "hello"}})

    assert result.run_id == "run-000001"
    assert store.get_run(result.run_id).status == "succeeded"


def test_runtime_updates_supplied_sqlite_run_store_status(tmp_path) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "stored-sqlite-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Stored {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")

    result = InProcessRuntime(stdlib_registry(), run_store=store).run(graph, {"message": {"text": "hello"}})

    assert result.run_id == "run-000001"
    assert store.get_run(result.run_id).status == "succeeded"


def test_runtime_persists_deployment_provenance_with_run_record(tmp_path) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "production-provenance"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Provenance {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    store_path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(store_path)
    provenance = RunDeploymentProvenance(
        release_digest="sha256:" + ("1" * 64),
        deployment_revision_id="revision-1",
        physical_plan_hash="sha256:" + ("2" * 64),
        release_signature_digest="sha256:" + ("3" * 64),
    )

    result = InProcessRuntime(stdlib_registry(), run_store=store).run(
        graph,
        {"message": {"text": "hello"}},
        run_id="run-production-1",
        deployment_provenance=provenance,
    )
    store.close()
    reopened = SQLiteRunStore(store_path)
    persisted = reopened.get_run(result.run_id)
    reopened.close()

    assert persisted.deployment_provenance == provenance
    assert result.journal.records[0].payload["deploymentProvenance"] == provenance.canonical_value()


def test_runtime_rejects_incomplete_production_deployment_provenance() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "incomplete-production-provenance"},
        "spec": {"nodes": {}},
    }

    with pytest.raises(
        ValueError,
        match="production deployment provenance deployment_revision_id is required",
    ):
        InProcessRuntime(stdlib_registry()).run(
            graph,
            {},
            deployment_provenance=RunDeploymentProvenance(
                release_digest="sha256:" + ("1" * 64),
            ),
        )


def test_runtime_uses_requested_run_id_for_store_and_journal(tmp_path) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "requested-runtime-run-id"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Requested {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    run_store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    journal_path = tmp_path / "journal.sqlite3"

    result = InProcessRuntime(
        stdlib_registry(),
        run_store=run_store,
        journal_factory=lambda run_id: SQLiteExecutionJournal(journal_path, run_id),
    ).run(graph, {"message": {"text": "hello"}}, run_id="run-requested-runtime-1")
    persisted_journal = SQLiteExecutionJournal(journal_path, "run-requested-runtime-1")

    assert result.run_id == "run-requested-runtime-1"
    assert run_store.get_run("run-requested-runtime-1").status == "succeeded"
    assert persisted_journal.terminal_kind == "run_succeeded"
    assert [record.kind for record in persisted_journal.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]


def test_runtime_can_persist_execution_journal_with_factory(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "persisted-journal"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Journal {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    result = InProcessRuntime(
        stdlib_registry(),
        journal_factory=lambda run_id: SQLiteExecutionJournal(database, run_id),
    ).run(graph, {"message": {"text": "hello"}})
    persisted = SQLiteExecutionJournal(database, result.run_id)

    assert result.status == "succeeded"
    assert persisted.terminal_kind == "run_succeeded"
    assert [record.kind for record in persisted.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]


def test_runtime_journal_preserves_arbitrary_precision_numbers(tmp_path) -> None:
    journal = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-decimal")
    journal.append("run_started", {"huge": Decimal("1e400")})

    persisted = SQLiteExecutionJournal(tmp_path / "journal.sqlite3", "run-decimal")

    assert persisted.records[0].payload["huge"] == Decimal("1e400")


def test_stdlib_async_blocks_start_and_await_callback_operation() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-start-await"},
        "spec": {
            "interface": {
                "outputs": {
                    "operation": "graphblocks.ai/AsyncOperation@1",
                    "wait": "graphblocks.ai/AsyncWait@1",
                }
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeoutMs": 1_800_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "$output.operation"},
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "checkpoint": True,
                        "onTimeout": "fail",
                        "timeout": "30m",
                        "idempotencyKey": "idem-op-ci-1",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {},
        run_id="run-coding-1",
    )

    assert result.status == "waiting_callback"
    assert result.outputs["operation"]["state"] == "waiting_callback"
    assert result.outputs["operation"]["expires_at_unix_ms"] == 1_801_000
    assert result.checkpoint is not None
    assert result.checkpoint.wait_node == "waitCI"
    assert result.checkpoint.operation["operation_id"] == "op-ci-1"


def test_stdlib_async_start_operation_rejects_noncanonical_resume_token_hash() -> None:
    with pytest.raises(ValueError, match="resumeTokenHash must be a canonical sha256 digest"):
        stdlib_registry().resolve("async.start_operation@1")(
            {},
            {
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": "sha256:resume-token",
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "timeoutMs": 1_800_000,
                "resume": {
                    "requirePolicyReevaluation": True,
                    "requireBudgetReservation": True,
                    "requireReleaseCompatibility": True,
                    "requireOwnershipFence": True,
                },
                "attemptFencing": True,
            },
            {},
        )


def test_stdlib_async_await_callback_rejects_noncanonical_operation_resume_token_hash() -> None:
    operation = {
        "operation_id": "op-ci-1",
        "run_id": "run-coding-1",
        "node_id": "startCI",
        "attempt_id": "attempt-1",
        "kind": "ci_job",
        "state": "waiting_callback",
        "resume_token_hash": "sha256:resume-token",
        "idempotency_key": "idem-op-ci-1",
        "expected_schema": "schemas/CICallback@1",
        "created_at_unix_ms": 1_000,
        "submitted_at_unix_ms": 1_050,
        "expires_at_unix_ms": 1_801_000,
    }

    with pytest.raises(ValueError, match="input operation resume_token_hash must be a canonical sha256 digest"):
        stdlib_registry().resolve("async.await_callback@1")(
            {"operation": operation},
            {
                "checkpoint": True,
                "onTimeout": "fail",
                "timeout": "30m",
                "idempotencyKey": "idem-op-ci-1",
                "callback": {"schema": "schemas/CICallback@1"},
                "resume": {
                    "requirePolicyReevaluation": True,
                    "requireBudgetReservation": True,
                    "requireReleaseCompatibility": True,
                    "requireOwnershipFence": True,
                },
                "attemptFencing": True,
            },
            {},
        )


def test_stdlib_async_start_operation_rejects_ambiguous_wait_bounds() -> None:
    with pytest.raises(ValueError, match="must not define both timeout and infiniteWaitPolicy"):
        stdlib_registry().resolve("async.start_operation@1")(
            {},
            {
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "timeoutMs": 1_800_000,
                "infiniteWaitPolicy": "operator_review_required",
                "resume": {
                    "requirePolicyReevaluation": True,
                    "requireBudgetReservation": True,
                    "requireReleaseCompatibility": True,
                    "requireOwnershipFence": True,
                },
                "attemptFencing": True,
            },
            {},
        )


def test_stdlib_async_start_operation_rejects_absolute_and_relative_wait_bounds() -> None:
    with pytest.raises(ValueError, match="must not define both expiresAtUnixMs and timeout"):
        stdlib_registry().resolve("async.start_operation@1")(
            {},
            {
                "operationId": "op-ci-1",
                "runId": "run-coding-1",
                "nodeId": "startCI",
                "attemptId": "attempt-1",
                "kind": "ci_job",
                "providerOperationId": "gha-run-1",
                "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                "idempotencyKey": "idem-op-ci-1",
                "expectedSchema": "schemas/CICallback@1",
                "createdAtUnixMs": 1_000,
                "submittedAtUnixMs": 1_050,
                "expiresAtUnixMs": 1_801_000,
                "timeoutMs": 1_800_000,
                "resume": {
                    "requirePolicyReevaluation": True,
                    "requireBudgetReservation": True,
                    "requireReleaseCompatibility": True,
                    "requireOwnershipFence": True,
                },
                "attemptFencing": True,
            },
            {},
        )


def test_stdlib_async_await_callback_rejects_ambiguous_wait_bounds() -> None:
    registry = stdlib_registry()
    operation = registry.resolve("async.start_operation@1")(
        {},
        {
            "operationId": "op-ci-1",
            "runId": "run-coding-1",
            "nodeId": "startCI",
            "attemptId": "attempt-1",
            "kind": "ci_job",
            "providerOperationId": "gha-run-1",
            "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
            "idempotencyKey": "idem-op-ci-1",
            "expectedSchema": "schemas/CICallback@1",
            "createdAtUnixMs": 1_000,
            "submittedAtUnixMs": 1_050,
            "infiniteWaitPolicy": "operator_review_required",
            "resume": {
                "requirePolicyReevaluation": True,
                "requireBudgetReservation": True,
                "requireReleaseCompatibility": True,
                "requireOwnershipFence": True,
            },
            "attemptFencing": True,
        },
        {},
    )["operation"]

    with pytest.raises(ValueError, match="must not define both timeout and infiniteWaitPolicy"):
        registry.resolve("async.await_callback@1")(
            {"operation": operation},
            {
                "checkpoint": True,
                "onTimeout": "fail",
                "timeout": "30m",
                "infiniteWaitPolicy": "operator_review_required",
            },
            {},
        )


def test_stdlib_async_await_callback_rejects_operation_without_expected_schema() -> None:
    operation = {
        "operation_id": "op-ci-1",
        "run_id": "run-coding-1",
        "node_id": "waitCI",
        "attempt_id": "attempt-1",
        "kind": "ci_job",
        "state": "waiting_callback",
        "resume_token_hash": VALID_RESUME_TOKEN_HASH,
        "idempotency_key": "idem-op-ci-1",
    }

    with pytest.raises(TypeError, match="input operation.expected_schema must be a non-empty string"):
        stdlib_registry().resolve("async.await_callback@1")(
            {"operation": operation},
            {"checkpoint": True, "onTimeout": "fail", "timeout": "30m"},
            {},
        )


def test_stdlib_async_await_callback_accepts_camel_case_operation_input() -> None:
    operation = {
        "operationId": "op-ci-1",
        "runId": "run-coding-1",
        "nodeId": "waitCI",
        "attemptId": "attempt-1",
        "kind": "ci_job",
        "state": "waiting_callback",
        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
        "idempotencyKey": "idem-op-ci-1",
        "expectedSchema": "schemas/CICallback@1",
        "submittedAtUnixMs": 1_050,
        "expiresAtUnixMs": 1_801_000,
    }

    wait = stdlib_registry().resolve("async.await_callback@1")(
        {"operation": operation},
        {"checkpoint": True, "onTimeout": "fail", "timeout": "30m"},
        {},
    )["wait"]

    assert wait["operation"]["operation_id"] == "op-ci-1"
    assert wait["operation"]["expected_schema"] == "schemas/CICallback@1"
    assert wait["operation"]["submitted_at_unix_ms"] == 1_050
    assert wait["operation"]["expires_at_unix_ms"] == 1_801_000


def test_stdlib_async_operation_consumers_accept_protocol_operation_projection() -> None:
    registry = stdlib_registry()
    operation = {
        "operationId": "op-ci-1",
        "runId": "run-coding-1",
        "nodeId": "waitCI",
        "attemptId": "attempt-1",
        "kind": "ci_job",
        "providerOperationId": "gha-run-1",
        "state": "waiting_callback",
        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
        "idempotencyKey": "idem-op-ci-1",
        "expectedSchema": "schemas/CICallback@1",
        "createdAtUnixMs": 1_000,
        "submittedAtUnixMs": 1_050,
        "expiresAtUnixMs": 1_801_000,
        "infiniteWaitPolicy": "operator_review_required",
    }

    wait = registry.resolve("async.await_callback@1")(
        {"operation": operation},
        {"checkpoint": True, "onTimeout": "fail", "timeout": "30m"},
        {},
    )["wait"]
    assert wait["operation"]["operation_id"] == "op-ci-1"
    assert wait["operation"]["provider_operation_id"] == "gha-run-1"
    assert wait["operation"]["created_at_unix_ms"] == 1_000
    assert wait["operation"]["submitted_at_unix_ms"] == 1_050
    assert wait["operation"]["expires_at_unix_ms"] == 1_801_000
    assert wait["operation"]["infinite_wait_policy"] == "operator_review_required"

    poll = registry.resolve("async.poll_operation@1")(
        {"operation": operation},
        {"interval": "30s", "timeout": "30m"},
        {},
    )["poll"]
    assert poll["operation"]["state"] == "polling"
    assert poll["operation"]["created_at_unix_ms"] == 1_000

    for block_name, config, expected_status in (
        ("async.complete_operation@1", {"completedAtUnixMs": 1_700, "artifacts": []}, "completed"),
        ("async.cancel_operation@1", {"cancelledAtUnixMs": 1_700}, "cancelled"),
        ("async.expire_operation@1", {"expiredAtUnixMs": 1_700}, "expired"),
    ):
        result = registry.resolve(block_name)({"operation": operation}, config, {})["result"]
        assert result["operation_id"] == "op-ci-1"
        assert result["status"] == expected_status
        assert result["completed_at_unix_ms"] == 1_700


def test_stdlib_async_poll_operation_rejects_ambiguous_wait_bounds() -> None:
    registry = stdlib_registry()
    operation = registry.resolve("async.start_operation@1")(
        {},
        {
            "operationId": "op-poll-1",
            "runId": "run-coding-1",
            "nodeId": "startPoll",
            "attemptId": "attempt-1",
            "kind": "external_provider_job",
            "providerOperationId": "batch-1",
            "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
            "idempotencyKey": "idem-op-poll-1",
            "expectedSchema": "schemas/PollResult@1",
            "createdAtUnixMs": 1_000,
            "submittedAtUnixMs": 1_050,
            "timeoutMs": 1_800_000,
            "resume": {
                "requirePolicyReevaluation": True,
                "requireBudgetReservation": True,
                "requireReleaseCompatibility": True,
                "requireOwnershipFence": True,
            },
            "attemptFencing": True,
        },
        {},
    )["operation"]

    with pytest.raises(ValueError, match="must not define both timeout and infiniteWaitPolicy"):
        registry.resolve("async.poll_operation@1")(
            {"operation": operation},
            {
                "interval": "30s",
                "maxInterval": "5m",
                "timeout": "2h",
                "infiniteWaitPolicy": "provider_has_no_timeout",
            },
            {},
        )


def test_stdlib_async_terminal_blocks_reject_invalid_terminal_timestamps() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-terminal-timestamp"},
        "spec": {
            "interface": {
                "outputs": {"cancelled": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-cancel",
                        "runId": "run-coding-1",
                        "nodeId": "startCancel",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-cancel",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-cancel",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "cancel.operation"},
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {"cancelledAtUnixMs": 1_000},
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "cancel"
    assert "async.cancel_operation@1 terminal timestamp" in failed[0].payload["error"]


def test_stdlib_async_terminal_blocks_reject_non_mapping_config() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-terminal-non-mapping-config"},
        "spec": {
            "interface": {
                "outputs": {"cancelled": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-cancel",
                        "runId": "run-coding-1",
                        "nodeId": "startCancel",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-cancel",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-cancel",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "cancel.operation"},
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": "invalid",
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"},
                },
            },
        },
    }

    with pytest.raises(
        ValueError,
        match=r"GB2019 \$\.spec\.nodes\.cancel\.config",
    ):
        InProcessRuntime(stdlib_registry()).run(graph, {})


def test_stdlib_async_terminal_blocks_reject_terminal_at_expiration() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-terminal-after-expiration"},
        "spec": {
            "interface": {
                "outputs": {"cancelled": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-cancel",
                        "runId": "run-coding-1",
                        "nodeId": "startCancel",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-cancel",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-cancel",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "cancel.operation"},
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {"cancelledAtUnixMs": 2_000},
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "cancel"
    assert "terminal timestamp must be earlier than expires_at_unix_ms" in failed[0].payload["error"]


def test_stdlib_async_poll_complete_and_cancel_preserve_terminal_projection_details() -> None:
    start_config = {
        "operationId": "op-placeholder",
        "runId": "run-coding-1",
        "nodeId": "node-placeholder",
        "attemptId": "attempt-1",
        "kind": "ci_job",
        "providerOperationId": "provider-placeholder",
        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
        "idempotencyKey": "idem-placeholder",
        "expectedSchema": "schemas/CICallback@1",
        "createdAtUnixMs": 1_000,
        "submittedAtUnixMs": 1_050,
        "expiresAtUnixMs": 2_000,
        "resume": {
            "requirePolicyReevaluation": True,
            "requireBudgetReservation": True,
            "requireReleaseCompatibility": True,
            "requireOwnershipFence": True,
        },
        "attemptFencing": True,
    }

    def config(operation_id: str, node_id: str) -> dict[str, object]:
        configured = dict(start_config)
        configured["operationId"] = operation_id
        configured["nodeId"] = node_id
        configured["providerOperationId"] = f"provider-{operation_id}"
        configured["resumeTokenHash"] = VALID_RESUME_TOKEN_HASH
        configured["idempotencyKey"] = f"idem-{operation_id}"
        return configured

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-terminal-details"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1",
                    "completed": "graphblocks.ai/AsyncOperationResult@1",
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1",
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"},
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "30s",
                        "maxInterval": "5m",
                        "timeout": "2h",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"},
                },
                "startComplete": {
                    "block": "async.start_operation@1",
                    "config": config("op-complete", "node-complete"),
                    "outputs": {"operation": "complete.operation"},
                },
                "complete": {
                    "block": "async.complete_operation@1",
                    "config": {
                        "completedAtUnixMs": 1_900,
                        "artifacts": [
                            {
                                "artifact_id": "artifact-ci-log",
                                "uri": "blob://ci/op-complete/log.json",
                                "media_type": "application/json",
                                "checksum": "sha256:ci-log",
                            }
                        ],
                        "diagnostics": [{"severity": "info", "message": "checks complete"}],
                        "metrics": [{"name": "duration_ms", "value": 840}],
                        "checks": [{"name": "unit", "status": "passed"}],
                        "usage": [{"kind": "ci_minutes", "amount": 2}],
                    },
                    "inputs": {
                        "operation": "startComplete.operation",
                        "output": "$input.payload",
                    },
                    "outputs": {"result": "$output.completed"},
                },
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"},
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {
                        "cancelledAtUnixMs": 1_900,
                        "externalEffects": [
                            {
                                "effectId": "effect-ticket-1",
                                "target": "ticket-system",
                                "operation": "ticket.create",
                                "outcome": "committed",
                                "idempotencyKey": "idem-ticket-1",
                                "providerEffectId": "ticket-123",
                            }
                        ],
                    },
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"payload": {"status": "completed"}})

    assert result.status == "succeeded"
    assert result.outputs["poll"]["intervalMs"] == 30_000
    assert result.outputs["poll"]["maxIntervalMs"] == 300_000
    assert result.outputs["poll"]["timeoutMs"] == 7_200_000
    assert result.outputs["completed"]["status"] == "completed"
    assert result.outputs["completed"]["output"] == {"status": "completed"}
    assert result.outputs["completed"]["completed_at_unix_ms"] == 1_900
    assert result.outputs["completed"]["artifacts"] == [
        {
            "artifact_id": "artifact-ci-log",
            "uri": "blob://ci/op-complete/log.json",
            "media_type": "application/json",
            "checksum": "sha256:ci-log",
        }
    ]
    assert result.outputs["completed"]["diagnostics"] == [{"severity": "info", "message": "checks complete"}]
    assert result.outputs["completed"]["metrics"] == [{"name": "duration_ms", "value": 840}]
    assert result.outputs["completed"]["checks"] == [{"name": "unit", "status": "passed"}]
    assert result.outputs["completed"]["usage"] == [{"kind": "ci_minutes", "amount": 2}]
    assert result.outputs["cancelled"]["status"] == "cancelled"
    assert result.outputs["cancelled"]["external_effects"] == [
        {
            "effect_id": "effect-ticket-1",
            "target": "ticket-system",
            "operation": "ticket.create",
            "outcome": "committed",
            "idempotency_key": "idem-ticket-1",
            "provider_effect_id": "ticket-123",
        }
    ]


def test_stdlib_async_terminal_effects_reject_provider_identity_without_committed_effect() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-effect-identity"},
        "spec": {
            "interface": {
                "outputs": {"cancelled": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-cancel",
                        "runId": "run-coding-1",
                        "nodeId": "startCancel",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-cancel",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-cancel",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "cancel.operation"},
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {
                        "cancelledAtUnixMs": 1_900,
                        "externalEffects": [
                            {
                                "effectId": "effect-ticket-1",
                                "target": "ticket-system",
                                "operation": "ticket.create",
                                "outcome": "no_external_effect",
                                "providerEffectId": "ticket-123",
                            }
                        ],
                    },
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "cancel"
    assert "provider identity but no committed external effect" in failed[0].payload["error"]


def test_stdlib_async_terminal_blocks_reject_invalid_projection_entries() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-result-projection"},
        "spec": {
            "interface": {
                "outputs": {"completed": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startComplete": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-complete",
                        "runId": "run-coding-1",
                        "nodeId": "startComplete",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-complete",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-complete",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "complete.operation"},
                },
                "complete": {
                    "block": "async.complete_operation@1",
                    "config": {
                        "completedAtUnixMs": 1_900,
                        "diagnostics": ["not-a-diagnostic-object"],
                    },
                    "inputs": {"operation": "startComplete.operation"},
                    "outputs": {"result": "$output.completed"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "complete"
    assert "config.diagnostics[0] must be a mapping" in failed[0].payload["error"]


def test_stdlib_async_terminal_blocks_reject_invalid_artifact_entries() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-result-artifact"},
        "spec": {
            "interface": {
                "outputs": {"completed": "graphblocks.ai/AsyncOperationResult@1"}
            },
            "nodes": {
                "startComplete": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-complete",
                        "runId": "run-coding-1",
                        "nodeId": "startComplete",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-complete",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-complete",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 2_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "complete.operation"},
                },
                "complete": {
                    "block": "async.complete_operation@1",
                    "config": {
                        "completedAtUnixMs": 1_900,
                        "artifacts": ["not-an-artifact-object"],
                    },
                    "inputs": {"operation": "startComplete.operation"},
                    "outputs": {"result": "$output.completed"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "complete"
    assert "config.artifacts[0] must be a mapping" in failed[0].payload["error"]


def test_stdlib_async_poll_rejects_max_interval_below_interval() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-poll-interval"},
        "spec": {
            "interface": {
                "outputs": {"poll": "graphblocks.ai/AsyncPoll@1"}
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-poll",
                        "runId": "run-coding-1",
                        "nodeId": "startPoll",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-poll",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-poll",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 10_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "poll.operation"},
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "5m",
                        "maxInterval": "30s",
                        "timeout": "2h",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "poll"
    assert "maxInterval must not be less than interval" in failed[0].payload["error"]


def test_stdlib_async_poll_rejects_oversized_string_duration() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-oversized-poll-duration"},
        "spec": {
            "interface": {
                "outputs": {"poll": "graphblocks.ai/AsyncPoll@1"}
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-poll",
                        "runId": "run-coding-1",
                        "nodeId": "startPoll",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-op-poll",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-poll",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 10_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "poll.operation"},
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "30s",
                        "timeout": "18446744073709551616ms",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "poll"
    assert "timeout must be an unsigned 64-bit duration" in failed[0].payload["error"]


def test_stdlib_async_await_callback_rejects_non_boolean_checkpoint() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-invalid-checkpoint"},
        "spec": {
            "interface": {
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeoutMs": 1_800_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "waitCI.operation"},
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "checkpoint": "yes",
                        "onTimeout": "fail",
                        "timeout": "30m",
                        "idempotencyKey": "idem-op-ci-1",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "waitCI"
    assert "checkpoint must be a boolean" in failed[0].payload["error"]


def test_stdlib_async_start_operation_rejects_timeout_expiration_overflow() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-timeout-overflow"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": (1 << 64) - 10,
                        "submittedAtUnixMs": (1 << 64) - 9,
                        "timeoutMs": 20,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "$output.operation"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "startCI"
    assert "timeout exceeds timestamp range" in failed[0].payload["error"]


def test_stdlib_async_start_operation_rejects_submitted_before_created() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-submitted-before-created"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 2_000,
                        "submittedAtUnixMs": 1_999,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "$output.operation"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "startCI"
    assert "submitted_at precedes created_at" in failed[0].payload["error"]


def test_stdlib_async_start_operation_rejects_expiry_before_submission() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-expiry-before-submission"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 2_500,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "$output.operation"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "startCI"
    assert "expires_at must be after submitted_at" in failed[0].payload["error"]


def test_stdlib_async_start_operation_rejects_wait_without_submission() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "python-stdlib-async-wait-without-submission"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "resumeTokenHash": VALID_RESUME_TOKEN_HASH,
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                    "outputs": {"operation": "$output.operation"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "startCI"
    assert "non-created operations require submitted_at" in failed[0].payload["error"]
