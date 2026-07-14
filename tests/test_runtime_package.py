from __future__ import annotations

import importlib.util
from decimal import Decimal
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


def load_runtime_wrapper(fake_native=None):
    package_root = ROOT / "packages" / "graphblocks-runtime" / "src" / "graphblocks_runtime"
    module_name = "_graphblocks_runtime_wrapper_under_test"
    native_module_name = f"{module_name}._native"
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if fake_native is not None:
        sys.modules[native_module_name] = fake_native
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(native_module_name, None)
    return module


def test_runtime_wrapper_reports_native_binding_readiness_without_second_implementation() -> None:
    runtime = load_runtime_wrapper()

    assert runtime.native_extension_available() is False
    status = runtime.native_extension_status()
    assert status == {
        "available": False,
        "binding_crate": "graphblocks-python",
        "binding_version": None,
        "module": "graphblocks_runtime._native",
        "error": status["error"],
    }
    assert "_native" in status["error"]
    assert "native_extension_available" in runtime.__all__
    assert "native_extension_status" in runtime.__all__
    assert "require_native_extension" in runtime.__all__
    with pytest.raises(RuntimeError, match="single PyO3 binding crate graphblocks-python"):
        runtime.require_native_extension()


def test_runtime_wrapper_rejects_non_standard_native_json_results() -> None:
    class FakeNative:
        __version__ = "0.1.0"

        def __getattr__(self, name: str):
            if name.endswith("_json"):
                return lambda *args, **kwargs: '{"ok":true}'
            raise AttributeError(name)

        def binding_version(self) -> str:
            return self.__version__

        def compile_graph_json(
            self,
            document_json: str,
            block_catalog_json: str | None = None,
            *,
            allow_unknown_blocks: bool = False,
        ) -> str:
            return '{"ok": NaN}'

    runtime = load_runtime_wrapper(FakeNative())

    with pytest.raises(ValueError, match="native compiler result must be valid strict JSON"):
        runtime.compile_graph({"kind": "Graph"})


def test_runtime_wrapper_preserves_exact_numbers_and_rejects_nonfinite_inputs() -> None:
    calls: list[str] = []

    class FakeNative:
        __version__ = "0.1.0"

        def __getattr__(self, name: str):
            if name.endswith("_json"):
                return lambda *args, **kwargs: '{"ok":true}'
            raise AttributeError(name)

        def binding_version(self) -> str:
            return self.__version__

        def compile_graph_json(
            self,
            document_json: str,
            block_catalog_json: str | None = None,
            *,
            allow_unknown_blocks: bool = False,
        ) -> str:
            calls.append(document_json)
            return '{"ok":true,"huge":1e400,"precise":1.234567890123456789}'

    runtime = load_runtime_wrapper(FakeNative())

    result = runtime.compile_graph(
        {"kind": "Graph", "value": Decimal("1.234567890123456789")}
    )
    assert "1.234567890123456789" in calls[0]
    assert result["huge"] == Decimal("1e400")
    assert result["precise"] == Decimal("1.234567890123456789")

    with pytest.raises(ValueError, match="Out of range float values"):
        runtime.compile_graph({"kind": "Graph", "value": float("nan")})
    with pytest.raises(ValueError, match="finite numbers"):
        runtime.compile_graph({"kind": "Graph", "value": Decimal("Infinity")})


def test_runtime_wrapper_requires_explicit_unknown_block_discovery() -> None:
    calls: list[tuple[str | None, bool]] = []

    class FakeNative:
        __version__ = "0.1.0"

        def __getattr__(self, name: str):
            if name.endswith("_json"):
                return lambda *args, **kwargs: '{"ok":true}'
            raise AttributeError(name)

        def binding_version(self) -> str:
            return self.__version__

        def compile_graph_json(
            self,
            document_json: str,
            block_catalog_json: str | None = None,
            *,
            allow_unknown_blocks: bool = False,
        ) -> str:
            calls.append((block_catalog_json, allow_unknown_blocks))
            return json.dumps({"ok": allow_unknown_blocks})

    runtime = load_runtime_wrapper(FakeNative())

    assert runtime.compile_graph({"kind": "Graph"}) == {"ok": False}
    assert runtime.compile_graph(
        {"kind": "Graph"},
        allow_unknown_blocks=True,
    ) == {"ok": True}
    assert calls == [(None, False), (None, True)]
    with pytest.raises(TypeError, match="allow_unknown_blocks must be a boolean"):
        runtime.compile_graph({"kind": "Graph"}, allow_unknown_blocks=1)


def test_runtime_wrapper_convenience_helpers_delegate_to_native_json() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def compile_graph_json(
        document_json: str,
        block_catalog_json: str | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> str:
        calls.append(
            (
                "compile",
                (document_json, block_catalog_json or "", allow_unknown_blocks),
            )
        )
        return json.dumps({"ok": True, "graph": json.loads(document_json), "diagnostics": []})

    def capture_telemetry_content_json(decision_json: str, content_json: str) -> str:
        calls.append(("telemetry_capture", (decision_json, content_json)))
        return json.dumps(
            {
                "decision": json.loads(decision_json),
                "content": json.loads(content_json),
                "contentDigest": "sha256:content",
            }
        )

    def evaluate_connector_capabilities_json(
        connection_json: str,
        required_capabilities_json: str,
    ) -> str:
        calls.append(("connector_capabilities", (connection_json, required_capabilities_json)))
        return json.dumps(
            {
                "ok": True,
                "connection": json.loads(connection_json),
                "requiredCapabilities": json.loads(required_capabilities_json),
                "supportedCapabilities": ["http_json", "oauth2"],
                "missingCapabilities": [],
                "error": None,
            }
        )

    def evaluate_tool_approval_json(
        record_json: str,
        resolved_tool_json: str,
        call_json: str,
        principal_id: str,
        now_unix_ms: int,
    ) -> str:
        calls.append(("tool_approval", (record_json, resolved_tool_json, call_json, principal_id, now_unix_ms)))
        return json.dumps(
            {
                "ok": True,
                "record": json.loads(record_json),
                "resolvedTool": json.loads(resolved_tool_json),
                "call": json.loads(call_json),
                "principalId": principal_id,
                "nowUnixMs": now_unix_ms,
                "recordValid": True,
                "validForCall": True,
            }
        )

    def evaluate_tool_admission_json(request_json: str) -> str:
        calls.append(("tool_admission", (request_json,)))
        return json.dumps(
            {
                "ok": True,
                "request": json.loads(request_json),
                "admitted": {"call": {"status": "admitted"}, "idempotencyKey": "idem-1"},
                "error": None,
            }
        )

    def evaluate_tool_resolution_json(
        catalog_json: str,
        scope_json: str,
        effective_policy_snapshot_id: str,
    ) -> str:
        calls.append(("tool_resolution", (catalog_json, scope_json, effective_policy_snapshot_id)))
        return json.dumps(
            {
                "ok": True,
                "catalog": json.loads(catalog_json),
                "scope": json.loads(scope_json),
                "effectivePolicySnapshotId": effective_policy_snapshot_id,
                "resolvedTools": [{"definition": {"name": "knowledge.search"}}],
                "error": None,
            }
        )

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        calls.append(("run_stdlib", (graph_json, inputs_json)))
        return json.dumps(
            {
                "runId": "run-native-1",
                "status": "succeeded",
                "outputs": {"answer": "ok"},
            }
        )

    def run_stdlib_graph_with_options_json(graph_json: str, inputs_json: str, options_json: str) -> str:
        calls.append(("run_stdlib_options", (graph_json, inputs_json, options_json)))
        options = json.loads(options_json)
        return json.dumps(
            {
                "runId": options["runId"],
                "status": "succeeded",
                "outputs": {"answer": "ok"},
            }
        )

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        calls.append(("run_test", (graph_json, inputs_json, node_outputs_json)))
        return json.dumps({"runId": "run-test-1", "status": "succeeded", "outputs": {"fixture": True}})

    def run_test_graph_with_options_json(
        graph_json: str,
        inputs_json: str,
        node_outputs_json: str,
        options_json: str,
    ) -> str:
        calls.append(("run_test_options", (graph_json, inputs_json, node_outputs_json, options_json)))
        options = json.loads(options_json)
        return json.dumps(
            {"runId": options["runId"], "status": "succeeded", "outputs": {"fixture": True}}
        )

    def finalize_tool_call_json(draft_json: str, resolved_tool_id: str, created_at_unix_ms: int) -> str:
        calls.append(("finalize_tool", (draft_json, resolved_tool_id, created_at_unix_ms)))
        return json.dumps(
            {
                "toolCallId": json.loads(draft_json)["toolCallId"],
                "resolvedToolId": resolved_tool_id,
                "createdAtUnixMs": created_at_unix_ms,
            }
        )

    def negotiate_application_protocol_capabilities_json(
        server_json: str,
        client_json: str,
    ) -> str:
        calls.append(("application_protocol_capabilities", (server_json, client_json)))
        server = json.loads(server_json)
        protocol_version = server.get("protocolVersion", server.get("protocol_version", ""))
        if isinstance(protocol_version, str) and not protocol_version.strip():
            raise ValueError(
                "application protocol capability negotiation failed: "
                "application protocol metadata field protocol_version must not be empty"
            )
        return json.dumps(
            {"ok": True, "server": server, "client": json.loads(client_json)}
        )

    def prepare_tool_result_for_model_json(
        call_json: str,
        result_json: str,
        resolved_tool_json: str,
        schema_registry_json: str,
        content_policy_json: str | None = None,
    ) -> str:
        calls.append(
            (
                "tool_result",
                (
                    call_json,
                    result_json,
                    resolved_tool_json,
                    schema_registry_json,
                    content_policy_json,
                ),
            )
        )
        return json.dumps(
            {
                "ok": True,
                "call": json.loads(call_json),
                "result": json.loads(result_json),
                "resolvedTool": json.loads(resolved_tool_json),
                "schemaRegistry": json.loads(schema_registry_json),
                "contentPolicy": None if content_policy_json is None else json.loads(content_policy_json),
            }
        )

    def record_tool_effect_precondition_json(
        resolved_tool_json: str,
        call_json: str,
        effect_key: str | None = None,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
        execution_target: str | None = None,
        sandbox_id: str | None = None,
    ) -> str:
        calls.append(
            (
                "tool_effect_precondition",
                (
                    resolved_tool_json,
                    call_json,
                    effect_key,
                    idempotency_key,
                    policy_decision_id,
                    execution_target,
                    sandbox_id,
                ),
            )
        )
        return json.dumps(
            {
                "digest": "sha256:precondition",
                "payload": {
                    "resolvedTool": json.loads(resolved_tool_json),
                    "call": json.loads(call_json),
                    "effectKey": effect_key,
                },
            }
        )

    def record_tool_effect_audit_event_json(
        event_id: str,
        occurred_at: str,
        actor_json: str,
        resolved_tool_json: str,
        call_json: str,
        result_json: str,
        effect_key: str | None = None,
        precondition_digest: str | None = None,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
    ) -> str:
        calls.append(
            (
                "tool_effect_audit_event",
                (
                    event_id,
                    occurred_at,
                    actor_json,
                    resolved_tool_json,
                    call_json,
                    result_json,
                    effect_key,
                    precondition_digest,
                    idempotency_key,
                    policy_decision_id,
                ),
            )
        )
        return json.dumps(
            {
                "eventId": event_id,
                "occurredAt": occurred_at,
                "actor": json.loads(actor_json),
                "payload": {
                    "resolvedTool": json.loads(resolved_tool_json),
                    "call": json.loads(call_json),
                    "result": json.loads(result_json),
                    "effectKey": effect_key,
                    "preconditionDigest": precondition_digest,
                },
                "payloadDigest": "sha256:audit-event",
            }
        )

    def evaluate_output_gate_json(gate_json: str, operations_json: str) -> str:
        calls.append(("output_gate", (gate_json, operations_json)))
        return json.dumps({"gate": json.loads(gate_json), "updates": json.loads(operations_json)})

    def evaluate_retry_policy_json(policy_json: str, request_json: str) -> str:
        calls.append(("retry_policy", (policy_json, request_json)))
        return json.dumps(
            {
                "ok": True,
                "decision": "retry",
                "delayMs": 1_500,
                "reason": None,
                "policy": json.loads(policy_json),
                "request": json.loads(request_json),
            }
        )

    def evaluate_provider_limit_policy_json(policy_json: str, incident_json: str) -> str:
        calls.append(("provider_limit", (policy_json, incident_json)))
        return json.dumps(
            {
                "ok": True,
                "decision": "fallback",
                "delayMs": None,
                "target": "openai-compatible:gpt-economy",
                "requiresPolicyRecheck": True,
                "reason": None,
                "policy": json.loads(policy_json),
                "incident": json.loads(incident_json),
            }
        )

    def evaluate_timeout_deadline_json(policy_json: str, request_json: str) -> str:
        calls.append(("timeout_deadline", (policy_json, request_json)))
        return json.dumps(
            {
                "ok": True,
                "policy": json.loads(policy_json),
                "request": json.loads(request_json),
            }
        )

    def evaluate_readiness_json(signals_json: str, dependencies_json: str) -> str:
        calls.append(("readiness", (signals_json, dependencies_json)))
        return json.dumps(
            {
                "ok": True,
                "signals": json.loads(signals_json),
                "dependencies": json.loads(dependencies_json),
            }
        )

    def evaluate_scheduler_json(nodes_json: str, operations_json: str) -> str:
        calls.append(("scheduler", (nodes_json, operations_json)))
        return json.dumps(
            {
                "ok": True,
                "nodes": json.loads(nodes_json),
                "operations": json.loads(operations_json),
            }
        )

    def evaluate_cancellation_scope_json(root_json: str, operations_json: str) -> str:
        calls.append(("cancellation_scope", (root_json, operations_json)))
        return json.dumps(
            {
                "ok": True,
                "root": json.loads(root_json),
                "operations": json.loads(operations_json),
            }
        )

    def evaluate_task_group_json(group_json: str, operations_json: str) -> str:
        calls.append(("task_group", (group_json, operations_json)))
        return json.dumps(
            {
                "ok": True,
                "group": json.loads(group_json),
                "operations": json.loads(operations_json),
            }
        )

    def evaluate_node_lifecycle_json(state_json: str, operations_json: str) -> str:
        calls.append(("node_lifecycle", (state_json, operations_json)))
        return json.dumps(
            {
                "ok": True,
                "state": json.loads(state_json),
                "operations": json.loads(operations_json),
            }
        )

    def evaluate_declarative_output_policy_json(
        rules_json: str,
        chunk_json: str,
        evaluated_at_unix_ms: int,
    ) -> str:
        calls.append(("output_policy", (rules_json, chunk_json, evaluated_at_unix_ms)))
        return json.dumps(
            {
                "disposition": "allow",
                "rules": json.loads(rules_json),
                "chunk": json.loads(chunk_json),
                "evaluatedAtUnixMs": evaluated_at_unix_ms,
            }
        )

    def evaluate_application_event_stream_json(state_json: str, operations_json: str) -> str:
        calls.append(("application_event_stream", (state_json, operations_json)))
        return json.dumps({"state": json.loads(state_json), "updates": json.loads(operations_json)})

    def evaluate_application_protocol_log_json(state_json: str, operations_json: str) -> str:
        calls.append(("application_protocol_log", (state_json, operations_json)))
        return json.dumps({"state": json.loads(state_json), "updates": json.loads(operations_json)})

    def evaluate_application_protocol_stream_json(state_json: str, operations_json: str) -> str:
        calls.append(("application_protocol_stream", (state_json, operations_json)))
        return json.dumps({"state": json.loads(state_json), "updates": json.loads(operations_json)})

    def evaluate_durable_tool_terminal_store_json(operations_json: str) -> str:
        calls.append(("durable_tool_terminal", (operations_json,)))
        return json.dumps({"operations": json.loads(operations_json)})

    def evaluate_tool_execution_plan_json(plan_json: str, operations_json: str) -> str:
        calls.append(("tool_execution_plan", (plan_json, operations_json)))
        return json.dumps({"plan": json.loads(plan_json), "operations": json.loads(operations_json)})

    def evaluate_tool_result_stream_json(state_json: str, operations_json: str) -> str:
        calls.append(("tool_result_stream", (state_json, operations_json)))
        return json.dumps({"state": json.loads(state_json), "updates": json.loads(operations_json)})

    def evaluate_sequential_tool_queue_json(queue_json: str, operations_json: str) -> str:
        calls.append(("sequential_tool_queue", (queue_json, operations_json)))
        return json.dumps({"queue": json.loads(queue_json), "operations": json.loads(operations_json)})

    def evaluate_usage_ledger_json(operations_json: str, run_id: str | None = None) -> str:
        calls.append(("usage_ledger", (operations_json, run_id)))
        return json.dumps(
            {
                "operations": json.loads(operations_json),
                "runId": run_id,
                "recordIds": ["usage-1"] if run_id else [],
            }
        )

    def evaluate_budget_ledger_json(operations_json: str) -> str:
        calls.append(("budget_ledger", (operations_json,)))
        return json.dumps({"ok": True, "operations": json.loads(operations_json)})

    def decide_agent_step_json(spec_json: str, request_json: str) -> str:
        calls.append(("agent_step", (spec_json, request_json)))
        return json.dumps({"decision": "continue", "spec": json.loads(spec_json), "request": json.loads(request_json)})

    def admit_exhaustion_work_json(policy_json: str, request_json: str) -> str:
        calls.append(("exhaustion", (policy_json, request_json)))
        return json.dumps({"allowed": True, "policy": json.loads(policy_json), "request": json.loads(request_json)})

    def admit_worker_message_json(
        message_json: str,
        daemon_config_json: str | None = None,
        response_message_id: str = "message-daemon-1",
        response_sequence: int = 1,
    ) -> str:
        calls.append(("worker_admission", (message_json, daemon_config_json, response_message_id, response_sequence)))
        return json.dumps(
            {
                "ok": True,
                "message": json.loads(message_json),
                "daemonConfig": None if daemon_config_json is None else json.loads(daemon_config_json),
                "responseMessageId": response_message_id,
                "responseSequence": response_sequence,
            }
        )

    def validate_worker_advertisement_json(
        advertisement_json: str,
        expected_package_lock_hash: str | None = None,
    ) -> str:
        calls.append(("worker_advertisement", (advertisement_json, expected_package_lock_hash)))
        return json.dumps({"ok": True, "advertisement": json.loads(advertisement_json)})

    def validate_worker_protocol_message_json(message_json: str) -> str:
        calls.append(("worker_message", (message_json,)))
        return json.dumps(
            {
                "ok": True,
                "message": json.loads(message_json),
                "contentDigest": "sha256:message",
            }
        )

    def validate_remote_payload_json(payload_json: str, max_inline_bytes: int) -> str:
        calls.append(("remote_payload", (payload_json, max_inline_bytes)))
        return json.dumps({"ok": True, "payload": json.loads(payload_json)})

    fake_native = SimpleNamespace(
        __version__="0.1.0",
        admit_exhaustion_work_json=admit_exhaustion_work_json,
        admit_worker_message_json=admit_worker_message_json,
        binding_version=lambda: "0.1.0",
        capture_telemetry_content_json=capture_telemetry_content_json,
        compile_graph_json=compile_graph_json,
        decide_agent_step_json=decide_agent_step_json,
        evaluate_application_event_stream_json=evaluate_application_event_stream_json,
        evaluate_application_protocol_log_json=evaluate_application_protocol_log_json,
        evaluate_application_protocol_stream_json=evaluate_application_protocol_stream_json,
        evaluate_budget_ledger_json=evaluate_budget_ledger_json,
        evaluate_cancellation_scope_json=evaluate_cancellation_scope_json,
        evaluate_connector_capabilities_json=evaluate_connector_capabilities_json,
        evaluate_declarative_output_policy_json=evaluate_declarative_output_policy_json,
        evaluate_durable_tool_terminal_store_json=evaluate_durable_tool_terminal_store_json,
        evaluate_node_lifecycle_json=evaluate_node_lifecycle_json,
        evaluate_output_gate_json=evaluate_output_gate_json,
        evaluate_provider_limit_policy_json=evaluate_provider_limit_policy_json,
        evaluate_readiness_json=evaluate_readiness_json,
        evaluate_retry_policy_json=evaluate_retry_policy_json,
        evaluate_scheduler_json=evaluate_scheduler_json,
        evaluate_sequential_tool_queue_json=evaluate_sequential_tool_queue_json,
        evaluate_task_group_json=evaluate_task_group_json,
        evaluate_timeout_deadline_json=evaluate_timeout_deadline_json,
        evaluate_tool_admission_json=evaluate_tool_admission_json,
        evaluate_tool_approval_json=evaluate_tool_approval_json,
        evaluate_tool_execution_plan_json=evaluate_tool_execution_plan_json,
        evaluate_tool_resolution_json=evaluate_tool_resolution_json,
        evaluate_tool_result_stream_json=evaluate_tool_result_stream_json,
        evaluate_usage_ledger_json=evaluate_usage_ledger_json,
        finalize_tool_call_json=finalize_tool_call_json,
        negotiate_application_protocol_capabilities_json=(
            negotiate_application_protocol_capabilities_json
        ),
        prepare_tool_result_for_model_json=prepare_tool_result_for_model_json,
        record_tool_effect_audit_event_json=record_tool_effect_audit_event_json,
        record_tool_effect_precondition_json=record_tool_effect_precondition_json,
        run_stdlib_graph_json=run_stdlib_graph_json,
        run_stdlib_graph_with_options_json=run_stdlib_graph_with_options_json,
        run_test_graph_json=run_test_graph_json,
        run_test_graph_with_options_json=run_test_graph_with_options_json,
        validate_remote_payload_json=validate_remote_payload_json,
        validate_worker_advertisement_json=validate_worker_advertisement_json,
        validate_worker_protocol_message_json=validate_worker_protocol_message_json,
    )
    runtime = load_runtime_wrapper(fake_native)

    captured_content = runtime.capture_telemetry_content(
        {"mode": "redacted_preview", "retentionPolicy": "debug-7d"},
        {
            "contentKind": "tool_result",
            "text": "safe prefix secret suffix",
            "redactions": [{"pattern": "secret", "replacement": "[redacted]"}],
        },
    )
    connector_capabilities = runtime.evaluate_connector_capabilities(
        {
            "connectionId": "ticket-system",
            "kind": "openapi",
            "provider": "zendesk",
            "supportedCapabilities": ["http_json", "oauth2"],
        },
        ["http_json"],
    )
    compiled = runtime.compile_graph({"kind": "Graph"}, block_catalog=[{"typeId": "prompt.render"}])
    stdlib = runtime.run_stdlib_graph({"kind": "Graph"}, {"message": {"text": "hi"}})
    stdlib_requested = runtime.run_stdlib_graph(
        {"kind": "Graph"},
        {"message": {"text": "hi"}},
        run_id="run-requested-native-1",
        run_store_path="/tmp/graphblocks-runs.sqlite3",
        journal_store_path="/tmp/graphblocks-journal.sqlite3",
        checkpoint_store_path="/tmp/graphblocks-checkpoints.sqlite3",
        async_operation_store_path="/tmp/graphblocks-operations.sqlite3",
        callback_receipt={"operation_id": "operation-1"},
        callback_admission_hmac_key="callback-admission-key-material-0001",
        deployment_provenance={
            "release_digest": "sha256:release",
            "deployment_revision_id": "revision-1",
            "physical_plan_hash": "sha256:physical-plan",
            "release_signature_digest": "sha256:signature",
        },
    )
    test_run = runtime.run_test_graph({"kind": "Graph"}, {"message": "hi"}, {"node": {"value": "ok"}})
    test_run_requested = runtime.run_test_graph(
        {"kind": "Graph"},
        {"message": "hi"},
        {"node": {"value": "ok"}},
        run_id="run-test-requested-1",
        run_store_path="/tmp/graphblocks-test-run.sqlite3",
        journal_store_path="/tmp/graphblocks-test-journal.sqlite3",
        deployment_provenance={
            "release_digest": "sha256:release",
            "deployment_revision_id": "revision-1",
            "physical_plan_hash": "sha256:physical-plan",
            "release_signature_digest": "sha256:signature",
        },
    )
    finalized = runtime.finalize_tool_call(
        {
            "toolCallId": "call-1",
            "responseId": "response-1",
            "toolName": "knowledge.search",
            "status": "arguments_complete",
            "argumentFragments": ["{}"],
            "sequence": 1,
        },
        resolved_tool_id="resolved-tool-1",
        created_at_unix_ms=1_000,
    )
    prepared_tool_result = runtime.prepare_tool_result_for_model(
        {"toolCallId": "call-1", "resolvedToolId": "resolved-tool-1"},
        {"toolCallId": "call-1", "status": "completed", "output": []},
        {"resolvedToolId": "resolved-tool-1"},
        [{"schemaId": "schemas/SearchResult@1"}],
        content_policy={"maxOutputBytes": 128},
    )
    tool_approval = runtime.evaluate_tool_approval(
        {
            "approvalId": "approval-1",
            "request": {
                "approvalId": "approval-1",
                "toolCallId": "call-1",
            },
            "status": "approved",
        },
        {"resolvedToolId": "resolved-tool-1"},
        {"toolCallId": "call-1"},
        principal_id="user-1",
        now_unix_ms=1_500,
    )
    tool_admission = runtime.evaluate_tool_admission(
        {
            "call": {"toolCallId": "call-1"},
            "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
            "schemaRegistry": [],
            "policyDecision": {"decisionId": "decision-1"},
            "principalId": "user-1",
            "admittedAtUnixMs": 1_200,
        }
    )
    tool_resolution = runtime.evaluate_tool_resolution(
        {
            "definitions": [{"name": "knowledge.search"}],
            "bindings": [{"bindingId": "binding-search"}],
        },
        {"applicationTools": ["knowledge.search"]},
        effective_policy_snapshot_id="policy-snapshot-1",
    )
    tool_effect_precondition = runtime.record_tool_effect_precondition(
        {"resolvedToolId": "resolved-tool-1"},
        {"toolCallId": "call-1", "status": "admitted"},
        effect_key="ticket.create:cust-1",
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
        execution_target="worker:local",
        sandbox_id="sandbox-1",
    )
    tool_effect_audit_event = runtime.record_tool_effect_audit_event(
        event_id="audit-effect-1",
        occurred_at="2026-06-23T00:00:02Z",
        actor={"principalId": "user-1"},
        resolved_tool={"resolvedToolId": "resolved-tool-1"},
        call={"toolCallId": "call-1"},
        result={"toolCallId": "call-1", "status": "completed"},
        effect_key="ticket.create:cust-1",
        precondition_digest="sha256:precondition",
        idempotency_key="idem-ticket-1",
        policy_decision_id="decision-tool-1",
    )
    gate_result = runtime.evaluate_output_gate(
        {"streamId": "stream-1"},
        [{"op": "chunk", "chunk": {"sequence": 1}}],
    )
    retry_decision = runtime.evaluate_retry_policy(
        {
            "maxAttempts": 3,
            "retryOn": ["timeout"],
            "backoff": {"kind": "fixed", "delayMs": 250},
        },
        {
            "attempt": 1,
            "error": {
                "code": "provider.timeout",
                "category": "timeout",
                "message": "timed out",
                "retryable": True,
            },
            "retryAfterMs": 1_500,
        },
    )
    provider_limit = runtime.evaluate_provider_limit_policy(
        {"fallbackEnabled": True, "queueEnabled": True},
        {
            "kind": "provider_quota_exceeded",
            "compatibleFallbacks": ["openai-compatible:gpt-economy"],
        },
    )
    timeout_deadline = runtime.evaluate_timeout_deadline(
        {"durationMs": 250},
        {"nodeId": "model", "startedAtMs": 1_000, "nowMs": 1_250},
    )
    readiness = runtime.evaluate_readiness(
        [
            {
                "node": "render",
                "port": "prompt",
                "outcome": {"status": "value", "value": "hello"},
            }
        ],
        [{"input": "prompt", "source": {"node": "render", "port": "prompt"}}],
    )
    scheduler = runtime.evaluate_scheduler(
        [{"nodeId": "render"}],
        [{"op": "admit_run"}],
    )
    cancellation_scope = runtime.evaluate_cancellation_scope(
        {"tokenId": "run", "scope": "run", "guarantee": "cooperative"},
        [
            {
                "op": "child",
                "parentId": "run",
                "tokenId": "provider",
                "scope": "provider_call",
                "guarantee": "best_effort_remote",
            },
            {"op": "cancel", "tokenId": "run", "reason": {"code": "policy_denied"}},
        ],
    )
    task_group = runtime.evaluate_task_group(
        {
            "children": ["dense", "keyword"],
            "policy": {
                "minimumSuccesses": 2,
                "failure": "fail_fast",
                "cancellation": "cancel_siblings_on_fatal",
            },
        },
        [
            {"op": "start", "childId": "dense"},
            {
                "op": "fail",
                "childId": "dense",
                "error": {
                    "code": "provider.timeout",
                    "category": "timeout",
                    "message": "provider timed out",
                    "retryable": True,
                },
            },
        ],
    )
    node_lifecycle = runtime.evaluate_node_lifecycle(
        {"initialStatus": "running"},
        [
            {"op": "output", "port": "value", "value": "before-terminal"},
            {"op": "complete"},
            {"op": "output", "port": "value", "value": "after-terminal"},
        ],
    )
    policy_decision = runtime.evaluate_declarative_output_policy(
        [{"ruleId": "allow"}],
        {"streamId": "stream-1", "sequence": 1},
        evaluated_at_unix_ms=1_010,
    )
    application_event_stream = runtime.evaluate_application_event_stream(
        {"acceptedEvents": []},
        [{"kind": "event", "event": {"kind": "RunStarted", "metadata": {"eventId": "event-1"}}}],
    )
    application_protocol_log = runtime.evaluate_application_protocol_log(
        {"events": []},
        [{"kind": "replay_after", "cursor": "cursor-1", "limit": 10}],
    )
    application_protocol_stream = runtime.evaluate_application_protocol_stream(
        {"acceptedEvents": []},
        [
            {
                "kind": "event",
                "event": {
                    "kind": "AssistantDraftDelta",
                    "metadata": {
                        "eventId": "event-1",
                        "protocolVersion": "graphblocks.app.v1",
                        "runId": "run-1",
                        "sequence": 1,
                        "occurredAtUnixMs": 1_000,
                    },
                    "payload": {"response_id": "response-1", "delta": "hello"},
                },
            }
        ],
    )
    protocol_capabilities = runtime.negotiate_application_protocol_capabilities(
        {
            "protocolVersion": "graphblocks.app.v1",
            "commands": ["InvokeGraph", "CancelRun"],
            "events": ["RunStarted", "RunCompleted"],
        },
        {
            "protocolVersion": "graphblocks.app.v1",
            "commands": ["CancelRun"],
            "events": ["RunCompleted"],
        },
    )
    durable_terminal = runtime.evaluate_durable_tool_terminal_store(
        [{"op": "tool_terminal_count"}],
    )
    tool_execution = runtime.evaluate_tool_execution_plan(
        {"planId": "plan-1", "maximumParallelism": 2},
        [{"op": "ready"}],
    )
    tool_result_stream = runtime.evaluate_tool_result_stream(
        {"acceptedEvents": []},
        [{"kind": "event", "event": {"kind": "started", "toolCallId": "call-1", "sequence": 1}}],
    )
    sequential_queue = runtime.evaluate_sequential_tool_queue(
        {"planId": "plan-1", "responseId": "response-1", "calls": []},
        [{"op": "start_next_ready"}],
    )
    usage_ledger = runtime.evaluate_usage_ledger(
        [{"op": "append", "record": {"recordId": "usage-1"}}],
        run_id="run-1",
    )
    budget_ledger = runtime.evaluate_budget_ledger(
        [{"op": "allocate", "budgetId": "budget-1"}],
    )
    agent_decision = runtime.decide_agent_step(
        {"maxSteps": 4},
        {"step": 2, "pendingToolCalls": 0},
    )
    exhaustion = runtime.admit_exhaustion_work(
        {"preset": "finish_current_turn"},
        {"workKind": "read_only_tool"},
    )
    worker_admission = runtime.admit_worker_message(
        {"messageId": "message-1", "kind": "advertisement"},
        daemon_config={"daemonId": "daemon-1"},
        response_message_id="message-daemon-1",
        response_sequence=2,
    )
    worker = runtime.validate_worker_advertisement(
        {"workerId": "worker-1"},
        expected_package_lock_hash="sha256:lock",
    )
    worker_message = runtime.validate_worker_protocol_message(
        {
            "messageId": "message-1",
            "kind": "invoke_request",
        },
    )
    remote_payload = runtime.validate_remote_payload(
        {"kind": "inline_json", "value": {"ok": True}},
        max_inline_bytes=128,
    )

    assert runtime.native_extension_available() is True
    assert captured_content == {
        "decision": {"mode": "redacted_preview", "retentionPolicy": "debug-7d"},
        "content": {
            "contentKind": "tool_result",
            "redactions": [{"pattern": "secret", "replacement": "[redacted]"}],
            "text": "safe prefix secret suffix",
        },
        "contentDigest": "sha256:content",
    }
    assert connector_capabilities == {
        "ok": True,
        "connection": {
            "connectionId": "ticket-system",
            "kind": "openapi",
            "provider": "zendesk",
            "supportedCapabilities": ["http_json", "oauth2"],
        },
        "requiredCapabilities": ["http_json"],
        "supportedCapabilities": ["http_json", "oauth2"],
        "missingCapabilities": [],
        "error": None,
    }
    assert compiled["ok"] is True
    assert stdlib["outputs"] == {"answer": "ok"}
    assert stdlib_requested["runId"] == "run-requested-native-1"
    assert stdlib_requested["outputs"] == {"answer": "ok"}
    assert test_run["outputs"] == {"fixture": True}
    assert test_run_requested["runId"] == "run-test-requested-1"
    assert finalized == {
        "toolCallId": "call-1",
        "resolvedToolId": "resolved-tool-1",
        "createdAtUnixMs": 1_000,
    }
    assert prepared_tool_result == {
        "ok": True,
        "call": {"toolCallId": "call-1", "resolvedToolId": "resolved-tool-1"},
        "result": {"toolCallId": "call-1", "status": "completed", "output": []},
        "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
        "schemaRegistry": [{"schemaId": "schemas/SearchResult@1"}],
        "contentPolicy": {"maxOutputBytes": 128},
    }
    assert tool_approval == {
        "ok": True,
        "record": {
            "approvalId": "approval-1",
            "request": {
                "approvalId": "approval-1",
                "toolCallId": "call-1",
            },
            "status": "approved",
        },
        "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
        "call": {"toolCallId": "call-1"},
        "principalId": "user-1",
        "nowUnixMs": 1_500,
        "recordValid": True,
        "validForCall": True,
    }
    assert tool_admission == {
        "ok": True,
        "request": {
            "admittedAtUnixMs": 1_200,
            "call": {"toolCallId": "call-1"},
            "policyDecision": {"decisionId": "decision-1"},
            "principalId": "user-1",
            "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
            "schemaRegistry": [],
        },
        "admitted": {"call": {"status": "admitted"}, "idempotencyKey": "idem-1"},
        "error": None,
    }
    assert tool_resolution == {
        "ok": True,
        "catalog": {
            "bindings": [{"bindingId": "binding-search"}],
            "definitions": [{"name": "knowledge.search"}],
        },
        "scope": {"applicationTools": ["knowledge.search"]},
        "effectivePolicySnapshotId": "policy-snapshot-1",
        "resolvedTools": [{"definition": {"name": "knowledge.search"}}],
        "error": None,
    }
    assert tool_effect_precondition == {
        "digest": "sha256:precondition",
        "payload": {
            "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
            "call": {"status": "admitted", "toolCallId": "call-1"},
            "effectKey": "ticket.create:cust-1",
        },
    }
    assert tool_effect_audit_event == {
        "eventId": "audit-effect-1",
        "occurredAt": "2026-06-23T00:00:02Z",
        "actor": {"principalId": "user-1"},
        "payload": {
            "resolvedTool": {"resolvedToolId": "resolved-tool-1"},
            "call": {"toolCallId": "call-1"},
            "result": {"status": "completed", "toolCallId": "call-1"},
            "effectKey": "ticket.create:cust-1",
            "preconditionDigest": "sha256:precondition",
        },
        "payloadDigest": "sha256:audit-event",
    }
    assert gate_result == {
        "gate": {"streamId": "stream-1"},
        "updates": [{"op": "chunk", "chunk": {"sequence": 1}}],
    }
    assert retry_decision == {
        "ok": True,
        "decision": "retry",
        "delayMs": 1_500,
        "reason": None,
        "policy": {
            "backoff": {"delayMs": 250, "kind": "fixed"},
            "maxAttempts": 3,
            "retryOn": ["timeout"],
        },
        "request": {
            "attempt": 1,
            "error": {
                "category": "timeout",
                "code": "provider.timeout",
                "message": "timed out",
                "retryable": True,
            },
            "retryAfterMs": 1_500,
        },
    }
    assert provider_limit == {
        "ok": True,
        "decision": "fallback",
        "delayMs": None,
        "target": "openai-compatible:gpt-economy",
        "requiresPolicyRecheck": True,
        "reason": None,
        "policy": {"fallbackEnabled": True, "queueEnabled": True},
        "incident": {
            "compatibleFallbacks": ["openai-compatible:gpt-economy"],
            "kind": "provider_quota_exceeded",
        },
    }
    assert timeout_deadline == {
        "ok": True,
        "policy": {"durationMs": 250},
        "request": {"nodeId": "model", "nowMs": 1_250, "startedAtMs": 1_000},
    }
    assert readiness == {
        "ok": True,
        "signals": [
            {
                "node": "render",
                "outcome": {"status": "value", "value": "hello"},
                "port": "prompt",
            }
        ],
        "dependencies": [{"input": "prompt", "source": {"node": "render", "port": "prompt"}}],
    }
    assert scheduler == {
        "ok": True,
        "nodes": [{"nodeId": "render"}],
        "operations": [{"op": "admit_run"}],
    }
    assert cancellation_scope == {
        "ok": True,
        "root": {"guarantee": "cooperative", "scope": "run", "tokenId": "run"},
        "operations": [
            {
                "guarantee": "best_effort_remote",
                "op": "child",
                "parentId": "run",
                "scope": "provider_call",
                "tokenId": "provider",
            },
            {"op": "cancel", "reason": {"code": "policy_denied"}, "tokenId": "run"},
        ],
    }
    assert task_group == {
        "ok": True,
        "group": {
            "children": ["dense", "keyword"],
            "policy": {
                "cancellation": "cancel_siblings_on_fatal",
                "failure": "fail_fast",
                "minimumSuccesses": 2,
            },
        },
        "operations": [
            {"childId": "dense", "op": "start"},
            {
                "childId": "dense",
                "error": {
                    "category": "timeout",
                    "code": "provider.timeout",
                    "message": "provider timed out",
                    "retryable": True,
                },
                "op": "fail",
            },
        ],
    }
    assert node_lifecycle == {
        "ok": True,
        "state": {"initialStatus": "running"},
        "operations": [
            {"op": "output", "port": "value", "value": "before-terminal"},
            {"op": "complete"},
            {"op": "output", "port": "value", "value": "after-terminal"},
        ],
    }
    assert policy_decision == {
        "disposition": "allow",
        "rules": [{"ruleId": "allow"}],
        "chunk": {"streamId": "stream-1", "sequence": 1},
        "evaluatedAtUnixMs": 1_010,
    }
    assert application_event_stream == {
        "state": {"acceptedEvents": []},
        "updates": [{"event": {"kind": "RunStarted", "metadata": {"eventId": "event-1"}}, "kind": "event"}],
    }
    assert application_protocol_log == {
        "state": {"events": []},
        "updates": [{"cursor": "cursor-1", "kind": "replay_after", "limit": 10}],
    }
    assert application_protocol_stream == {
        "state": {"acceptedEvents": []},
        "updates": [
            {
                "event": {
                    "kind": "AssistantDraftDelta",
                    "metadata": {
                        "eventId": "event-1",
                        "occurredAtUnixMs": 1_000,
                        "protocolVersion": "graphblocks.app.v1",
                        "runId": "run-1",
                        "sequence": 1,
                    },
                    "payload": {"delta": "hello", "response_id": "response-1"},
                },
                "kind": "event",
            }
        ],
    }
    assert protocol_capabilities == {
        "ok": True,
        "server": {
            "commands": ["InvokeGraph", "CancelRun"],
            "events": ["RunStarted", "RunCompleted"],
            "protocolVersion": "graphblocks.app.v1",
        },
        "client": {
            "commands": ["CancelRun"],
            "events": ["RunCompleted"],
            "protocolVersion": "graphblocks.app.v1",
        },
    }
    with pytest.raises(ValueError, match="protocol_version must not be empty"):
        runtime.negotiate_application_protocol_capabilities(
            {"protocolVersion": " ", "commands": ["InvokeGraph"], "events": ["RunStarted"]},
            {
                "protocolVersion": "graphblocks.app.v1",
                "commands": ["InvokeGraph"],
                "events": ["RunStarted"],
            },
        )
    assert durable_terminal == {"operations": [{"op": "tool_terminal_count"}]}
    assert tool_execution == {
        "plan": {"maximumParallelism": 2, "planId": "plan-1"},
        "operations": [{"op": "ready"}],
    }
    assert tool_result_stream == {
        "state": {"acceptedEvents": []},
        "updates": [{"event": {"kind": "started", "sequence": 1, "toolCallId": "call-1"}, "kind": "event"}],
    }
    assert sequential_queue == {
        "queue": {"calls": [], "planId": "plan-1", "responseId": "response-1"},
        "operations": [{"op": "start_next_ready"}],
    }
    assert usage_ledger == {
        "operations": [{"op": "append", "record": {"recordId": "usage-1"}}],
        "recordIds": ["usage-1"],
        "runId": "run-1",
    }
    assert budget_ledger == {
        "ok": True,
        "operations": [{"op": "allocate", "budgetId": "budget-1"}],
    }
    assert agent_decision == {
        "decision": "continue",
        "spec": {"maxSteps": 4},
        "request": {"step": 2, "pendingToolCalls": 0},
    }
    assert exhaustion == {
        "allowed": True,
        "policy": {"preset": "finish_current_turn"},
        "request": {"workKind": "read_only_tool"},
    }
    assert worker_admission == {
        "ok": True,
        "message": {"kind": "advertisement", "messageId": "message-1"},
        "daemonConfig": {"daemonId": "daemon-1"},
        "responseMessageId": "message-daemon-1",
        "responseSequence": 2,
    }
    assert worker == {"ok": True, "advertisement": {"workerId": "worker-1"}}
    assert worker_message == {
        "ok": True,
        "message": {"kind": "invoke_request", "messageId": "message-1"},
        "contentDigest": "sha256:message",
    }
    assert remote_payload == {"ok": True, "payload": {"kind": "inline_json", "value": {"ok": True}}}
    assert calls == [
        (
            "telemetry_capture",
            (
                '{"mode":"redacted_preview","retentionPolicy":"debug-7d"}',
                (
                    '{"contentKind":"tool_result","redactions":'
                    '[{"pattern":"secret","replacement":"[redacted]"}],'
                    '"text":"safe prefix secret suffix"}'
                ),
            ),
        ),
        (
            "connector_capabilities",
            (
                (
                    '{"connectionId":"ticket-system","kind":"openapi","provider":"zendesk",'
                    '"supportedCapabilities":["http_json","oauth2"]}'
                ),
                '["http_json"]',
            ),
        ),
        (
            "compile",
            (
                '{"kind":"Graph"}',
                '[{"typeId":"prompt.render"}]',
                False,
            ),
        ),
        ("run_stdlib", ('{"kind":"Graph"}', '{"message":{"text":"hi"}}')),
        (
            "run_stdlib_options",
            (
                '{"kind":"Graph"}',
                '{"message":{"text":"hi"}}',
                '{"asyncOperationStorePath":"/tmp/graphblocks-operations.sqlite3",'
                '"callbackAdmissionHmacKey":"callback-admission-key-material-0001",'
                '"callbackReceipt":{"operation_id":"operation-1"},'
                '"checkpointStorePath":"/tmp/graphblocks-checkpoints.sqlite3",'
                '"deploymentProvenance":{"deployment_revision_id":"revision-1",'
                '"physical_plan_hash":"sha256:physical-plan","release_digest":"sha256:release",'
                '"release_signature_digest":"sha256:signature"},'
                '"journalStorePath":"/tmp/graphblocks-journal.sqlite3",'
                '"runId":"run-requested-native-1","runStorePath":"/tmp/graphblocks-runs.sqlite3"}',
            ),
        ),
        ("run_test", ('{"kind":"Graph"}', '{"message":"hi"}', '{"node":{"value":"ok"}}')),
        (
            "run_test_options",
            (
                '{"kind":"Graph"}',
                '{"message":"hi"}',
                '{"node":{"value":"ok"}}',
                '{"deploymentProvenance":{"deployment_revision_id":"revision-1",'
                '"physical_plan_hash":"sha256:physical-plan","release_digest":"sha256:release",'
                '"release_signature_digest":"sha256:signature"},'
                '"journalStorePath":"/tmp/graphblocks-test-journal.sqlite3",'
                '"runId":"run-test-requested-1",'
                '"runStorePath":"/tmp/graphblocks-test-run.sqlite3"}',
            ),
        ),
        (
            "finalize_tool",
            (
                '{"argumentFragments":["{}"],"responseId":"response-1","sequence":1,'
                '"status":"arguments_complete","toolCallId":"call-1","toolName":"knowledge.search"}',
                "resolved-tool-1",
                1_000,
            ),
        ),
        (
            "tool_result",
            (
                '{"resolvedToolId":"resolved-tool-1","toolCallId":"call-1"}',
                '{"output":[],"status":"completed","toolCallId":"call-1"}',
                '{"resolvedToolId":"resolved-tool-1"}',
                '[{"schemaId":"schemas/SearchResult@1"}]',
                '{"maxOutputBytes":128}',
            ),
        ),
        (
            "tool_approval",
            (
                (
                    '{"approvalId":"approval-1","request":{"approvalId":"approval-1",'
                    '"toolCallId":"call-1"},"status":"approved"}'
                ),
                '{"resolvedToolId":"resolved-tool-1"}',
                '{"toolCallId":"call-1"}',
                "user-1",
                1_500,
            ),
        ),
        (
            "tool_admission",
            (
                (
                    '{"admittedAtUnixMs":1200,"call":{"toolCallId":"call-1"},'
                    '"policyDecision":{"decisionId":"decision-1"},"principalId":"user-1",'
                    '"resolvedTool":{"resolvedToolId":"resolved-tool-1"},"schemaRegistry":[]}'
                ),
            ),
        ),
        (
            "tool_resolution",
            (
                (
                    '{"bindings":[{"bindingId":"binding-search"}],'
                    '"definitions":[{"name":"knowledge.search"}]}'
                ),
                '{"applicationTools":["knowledge.search"]}',
                "policy-snapshot-1",
            ),
        ),
        (
            "tool_effect_precondition",
            (
                '{"resolvedToolId":"resolved-tool-1"}',
                '{"status":"admitted","toolCallId":"call-1"}',
                "ticket.create:cust-1",
                "idem-ticket-1",
                "decision-tool-1",
                "worker:local",
                "sandbox-1",
            ),
        ),
        (
            "tool_effect_audit_event",
            (
                "audit-effect-1",
                "2026-06-23T00:00:02Z",
                '{"principalId":"user-1"}',
                '{"resolvedToolId":"resolved-tool-1"}',
                '{"toolCallId":"call-1"}',
                '{"status":"completed","toolCallId":"call-1"}',
                "ticket.create:cust-1",
                "sha256:precondition",
                "idem-ticket-1",
                "decision-tool-1",
            ),
        ),
        (
            "output_gate",
            ('{"streamId":"stream-1"}', '[{"chunk":{"sequence":1},"op":"chunk"}]'),
        ),
        (
            "retry_policy",
            (
                '{"backoff":{"delayMs":250,"kind":"fixed"},"maxAttempts":3,"retryOn":["timeout"]}',
                (
                    '{"attempt":1,"error":{"category":"timeout","code":"provider.timeout",'
                    '"message":"timed out","retryable":true},"retryAfterMs":1500}'
                ),
            ),
        ),
        (
            "provider_limit",
            (
                '{"fallbackEnabled":true,"queueEnabled":true}',
                (
                    '{"compatibleFallbacks":["openai-compatible:gpt-economy"],'
                    '"kind":"provider_quota_exceeded"}'
                ),
            ),
        ),
        (
            "timeout_deadline",
            (
                '{"durationMs":250}',
                '{"nodeId":"model","nowMs":1250,"startedAtMs":1000}',
            ),
        ),
        (
            "readiness",
            (
                '[{"node":"render","outcome":{"status":"value","value":"hello"},"port":"prompt"}]',
                '[{"input":"prompt","source":{"node":"render","port":"prompt"}}]',
            ),
        ),
        (
            "scheduler",
            (
                '[{"nodeId":"render"}]',
                '[{"op":"admit_run"}]',
            ),
        ),
        (
            "cancellation_scope",
            (
                '{"guarantee":"cooperative","scope":"run","tokenId":"run"}',
                (
                    '[{"guarantee":"best_effort_remote","op":"child","parentId":"run",'
                    '"scope":"provider_call","tokenId":"provider"},'
                    '{"op":"cancel","reason":{"code":"policy_denied"},"tokenId":"run"}]'
                ),
            ),
        ),
        (
            "task_group",
            (
                (
                    '{"children":["dense","keyword"],"policy":{"cancellation":'
                    '"cancel_siblings_on_fatal","failure":"fail_fast","minimumSuccesses":2}}'
                ),
                (
                    '[{"childId":"dense","op":"start"},{"childId":"dense","error":'
                    '{"category":"timeout","code":"provider.timeout",'
                    '"message":"provider timed out","retryable":true},"op":"fail"}]'
                ),
            ),
        ),
        (
            "node_lifecycle",
            (
                '{"initialStatus":"running"}',
                (
                    '[{"op":"output","port":"value","value":"before-terminal"},'
                    '{"op":"complete"},{"op":"output","port":"value","value":"after-terminal"}]'
                ),
            ),
        ),
        (
            "output_policy",
            ('[{"ruleId":"allow"}]', '{"sequence":1,"streamId":"stream-1"}', 1_010),
        ),
        (
            "application_event_stream",
            (
                '{"acceptedEvents":[]}',
                '[{"event":{"kind":"RunStarted","metadata":{"eventId":"event-1"}},"kind":"event"}]',
            ),
        ),
        (
            "application_protocol_log",
            (
                '{"events":[]}',
                '[{"cursor":"cursor-1","kind":"replay_after","limit":10}]',
            ),
        ),
        (
            "application_protocol_stream",
            (
                '{"acceptedEvents":[]}',
                (
                    '[{"event":{"kind":"AssistantDraftDelta","metadata":'
                    '{"eventId":"event-1","occurredAtUnixMs":1000,'
                    '"protocolVersion":"graphblocks.app.v1","runId":"run-1","sequence":1},'
                    '"payload":{"delta":"hello","response_id":"response-1"}},"kind":"event"}]'
                ),
            ),
        ),
        (
            "application_protocol_capabilities",
            (
                (
                    '{"commands":["InvokeGraph","CancelRun"],'
                    '"events":["RunStarted","RunCompleted"],'
                    '"protocolVersion":"graphblocks.app.v1"}'
                ),
                (
                    '{"commands":["CancelRun"],"events":["RunCompleted"],'
                    '"protocolVersion":"graphblocks.app.v1"}'
                ),
            ),
        ),
        (
            "durable_tool_terminal",
            ('[{"op":"tool_terminal_count"}]',),
        ),
        (
            "tool_execution_plan",
            ('{"maximumParallelism":2,"planId":"plan-1"}', '[{"op":"ready"}]'),
        ),
        (
            "tool_result_stream",
            (
                '{"acceptedEvents":[]}',
                '[{"event":{"kind":"started","sequence":1,"toolCallId":"call-1"},"kind":"event"}]',
            ),
        ),
        (
            "sequential_tool_queue",
            (
                '{"calls":[],"planId":"plan-1","responseId":"response-1"}',
                '[{"op":"start_next_ready"}]',
            ),
        ),
        (
            "usage_ledger",
            ('[{"op":"append","record":{"recordId":"usage-1"}}]', "run-1"),
        ),
        (
            "budget_ledger",
            ('[{"budgetId":"budget-1","op":"allocate"}]',),
        ),
        (
            "agent_step",
            ('{"maxSteps":4}', '{"pendingToolCalls":0,"step":2}'),
        ),
        (
            "exhaustion",
            ('{"preset":"finish_current_turn"}', '{"workKind":"read_only_tool"}'),
        ),
        (
            "worker_admission",
            (
                '{"kind":"advertisement","messageId":"message-1"}',
                '{"daemonId":"daemon-1"}',
                "message-daemon-1",
                2,
            ),
        ),
        (
            "worker_advertisement",
            ('{"workerId":"worker-1"}', "sha256:lock"),
        ),
        (
            "worker_message",
            ('{"kind":"invoke_request","messageId":"message-1"}',),
        ),
        (
            "remote_payload",
            ('{"kind":"inline_json","value":{"ok":true}}', 128),
        ),
        (
            "application_protocol_capabilities",
            (
                '{"commands":["InvokeGraph"],"events":["RunStarted"],"protocolVersion":" "}',
                (
                    '{"commands":["InvokeGraph"],"events":["RunStarted"],'
                    '"protocolVersion":"graphblocks.app.v1"}'
                ),
            ),
        ),
    ]
    assert "admit_exhaustion_work" in runtime.__all__
    assert "admit_worker_message" in runtime.__all__
    assert "admit_worker_message_json" in runtime.__all__
    assert "compile_graph" in runtime.__all__
    assert "decide_agent_step" in runtime.__all__
    assert "run_stdlib_graph" in runtime.__all__
    assert "run_test_graph" in runtime.__all__
    assert "finalize_tool_call" in runtime.__all__
    assert "negotiate_application_protocol_capabilities" in runtime.__all__
    assert "negotiate_application_protocol_capabilities_json" in runtime.__all__
    assert "prepare_tool_result_for_model" in runtime.__all__
    assert "prepare_tool_result_for_model_json" in runtime.__all__
    assert "evaluate_output_gate" in runtime.__all__
    assert "evaluate_retry_policy" in runtime.__all__
    assert "evaluate_retry_policy_json" in runtime.__all__
    assert "evaluate_provider_limit_policy" in runtime.__all__
    assert "evaluate_provider_limit_policy_json" in runtime.__all__
    assert "evaluate_timeout_deadline" in runtime.__all__
    assert "evaluate_timeout_deadline_json" in runtime.__all__
    assert "evaluate_readiness" in runtime.__all__
    assert "evaluate_readiness_json" in runtime.__all__
    assert "evaluate_scheduler" in runtime.__all__
    assert "evaluate_scheduler_json" in runtime.__all__
    assert "evaluate_cancellation_scope" in runtime.__all__
    assert "evaluate_cancellation_scope_json" in runtime.__all__
    assert "evaluate_task_group" in runtime.__all__
    assert "evaluate_task_group_json" in runtime.__all__
    assert "evaluate_node_lifecycle" in runtime.__all__
    assert "evaluate_node_lifecycle_json" in runtime.__all__
    assert "evaluate_declarative_output_policy" in runtime.__all__
    assert "evaluate_application_event_stream" in runtime.__all__
    assert "evaluate_application_event_stream_json" in runtime.__all__
    assert "evaluate_application_protocol_log" in runtime.__all__
    assert "evaluate_application_protocol_log_json" in runtime.__all__
    assert "evaluate_application_protocol_stream" in runtime.__all__
    assert "evaluate_application_protocol_stream_json" in runtime.__all__
    assert "evaluate_connector_capabilities" in runtime.__all__
    assert "evaluate_connector_capabilities_json" in runtime.__all__
    assert "evaluate_durable_tool_terminal_store" in runtime.__all__
    assert "evaluate_durable_tool_terminal_store_json" in runtime.__all__
    assert "evaluate_tool_execution_plan" in runtime.__all__
    assert "evaluate_tool_execution_plan_json" in runtime.__all__
    assert "evaluate_tool_admission" in runtime.__all__
    assert "evaluate_tool_admission_json" in runtime.__all__
    assert "evaluate_tool_resolution" in runtime.__all__
    assert "evaluate_tool_resolution_json" in runtime.__all__
    assert "evaluate_tool_approval" in runtime.__all__
    assert "evaluate_tool_approval_json" in runtime.__all__
    assert "evaluate_tool_result_stream" in runtime.__all__
    assert "evaluate_tool_result_stream_json" in runtime.__all__
    assert "evaluate_sequential_tool_queue" in runtime.__all__
    assert "evaluate_sequential_tool_queue_json" in runtime.__all__
    assert "evaluate_usage_ledger" in runtime.__all__
    assert "evaluate_usage_ledger_json" in runtime.__all__
    assert "evaluate_budget_ledger" in runtime.__all__
    assert "evaluate_budget_ledger_json" in runtime.__all__
    assert "validate_worker_advertisement" in runtime.__all__
    assert "validate_worker_protocol_message" in runtime.__all__
    assert "validate_remote_payload" in runtime.__all__
