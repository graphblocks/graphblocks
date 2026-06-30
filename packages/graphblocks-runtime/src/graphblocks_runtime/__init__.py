from __future__ import annotations

import json

_NATIVE_EXTENSION_MODULE = "graphblocks_runtime._native"
_BINDING_CRATE = "graphblocks-python"

try:
    from ._native import (
        __version__,
        admit_exhaustion_work_json,
        admit_worker_message_json,
        binding_version,
        capture_telemetry_content_json,
        compile_graph_json,
        decide_agent_step_json,
        evaluate_application_event_stream_json,
        evaluate_application_protocol_log_json,
        evaluate_application_protocol_stream_json,
        evaluate_connector_capabilities_json,
        evaluate_declarative_output_policy_json,
        evaluate_durable_tool_terminal_store_json,
        evaluate_output_gate_json,
        evaluate_sequential_tool_queue_json,
        evaluate_tool_execution_plan_json,
        evaluate_tool_result_stream_json,
        evaluate_usage_ledger_json,
        finalize_tool_call_json,
        negotiate_application_protocol_capabilities_json,
        prepare_tool_result_for_model_json,
        record_tool_effect_audit_event_json,
        record_tool_effect_precondition_json,
        run_stdlib_graph_json,
        run_test_graph_json,
        validate_remote_payload_json,
        validate_worker_advertisement_json,
        validate_worker_protocol_message_json,
    )

    _NATIVE_EXTENSION_AVAILABLE = True
    _NATIVE_EXTENSION_ERROR: ImportError | None = None
except ImportError as error:
    __version__ = "0.1.0"
    _NATIVE_EXTENSION_AVAILABLE = False
    _NATIVE_EXTENSION_ERROR = error

    def native_extension_available() -> bool:
        return _NATIVE_EXTENSION_AVAILABLE

    def native_extension_status() -> dict[str, bool | str | None]:
        return {
            "available": _NATIVE_EXTENSION_AVAILABLE,
            "binding_crate": _BINDING_CRATE,
            "binding_version": None,
            "module": _NATIVE_EXTENSION_MODULE,
            "error": str(_NATIVE_EXTENSION_ERROR),
        }

    def require_native_extension() -> None:
        raise RuntimeError(
            "graphblocks_runtime native extension is not built; build graphblocks-runtime "
            "with maturin so the single PyO3 binding crate graphblocks-python is installed "
            "as graphblocks_runtime._native"
        )

    def binding_version() -> str:
        return __version__

    def capture_telemetry_content_json(decision_json: str, content_json: str) -> str:
        require_native_extension()

    def admit_exhaustion_work_json(policy_json: str, request_json: str) -> str:
        require_native_extension()

    def admit_worker_message_json(
        message_json: str,
        daemon_config_json: str | None = None,
        response_message_id: str = "message-daemon-1",
        response_sequence: int = 1,
    ) -> str:
        require_native_extension()

    def compile_graph_json(document_json: str, block_catalog_json: str | None = None) -> str:
        require_native_extension()

    def finalize_tool_call_json(
        draft_json: str,
        resolved_tool_id: str,
        created_at_unix_ms: int,
    ) -> str:
        require_native_extension()

    def negotiate_application_protocol_capabilities_json(
        server_json: str,
        client_json: str,
    ) -> str:
        require_native_extension()

    def prepare_tool_result_for_model_json(
        call_json: str,
        result_json: str,
        resolved_tool_json: str,
        schema_registry_json: str,
        content_policy_json: str | None = None,
    ) -> str:
        require_native_extension()

    def record_tool_effect_precondition_json(
        resolved_tool_json: str,
        call_json: str,
        effect_key: str | None = None,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
        execution_target: str | None = None,
        sandbox_id: str | None = None,
    ) -> str:
        require_native_extension()

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
        require_native_extension()

    def decide_agent_step_json(spec_json: str, request_json: str) -> str:
        require_native_extension()

    def evaluate_output_gate_json(gate_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_declarative_output_policy_json(
        rules_json: str,
        chunk_json: str,
        evaluated_at_unix_ms: int,
    ) -> str:
        require_native_extension()

    def evaluate_application_event_stream_json(state_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_application_protocol_log_json(state_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_application_protocol_stream_json(state_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_connector_capabilities_json(
        connection_json: str,
        required_capabilities_json: str,
    ) -> str:
        require_native_extension()

    def evaluate_durable_tool_terminal_store_json(operations_json: str) -> str:
        require_native_extension()

    def evaluate_tool_execution_plan_json(plan_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_tool_result_stream_json(state_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_sequential_tool_queue_json(queue_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_usage_ledger_json(operations_json: str, run_id: str | None = None) -> str:
        require_native_extension()

    def validate_worker_advertisement_json(
        advertisement_json: str,
        expected_package_lock_hash: str | None = None,
    ) -> str:
        require_native_extension()

    def validate_worker_protocol_message_json(message_json: str) -> str:
        require_native_extension()

    def validate_remote_payload_json(payload_json: str, max_inline_bytes: int) -> str:
        require_native_extension()

    def run_test_graph_json(graph_json: str, inputs_json: str, node_outputs_json: str) -> str:
        require_native_extension()

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        require_native_extension()
else:

    def native_extension_available() -> bool:
        return _NATIVE_EXTENSION_AVAILABLE

    def native_extension_status() -> dict[str, bool | str | None]:
        return {
            "available": _NATIVE_EXTENSION_AVAILABLE,
            "binding_crate": _BINDING_CRATE,
            "binding_version": binding_version(),
            "module": _NATIVE_EXTENSION_MODULE,
            "error": None,
        }

    def require_native_extension() -> None:
        return None


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_object_result(result_json: str, label: str) -> dict[str, object]:
    payload = json.loads(result_json)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return payload


def capture_telemetry_content(decision: dict[str, object], content: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        capture_telemetry_content_json(_canonical_json(decision), _canonical_json(content)),
        "native telemetry captured content",
    )


def compile_graph(document: dict[str, object], block_catalog: object | None = None) -> dict[str, object]:
    block_catalog_json = None if block_catalog is None else _canonical_json(block_catalog)
    return _json_object_result(
        compile_graph_json(_canonical_json(document), block_catalog_json),
        "native compiler result",
    )


def run_stdlib_graph(graph: dict[str, object], inputs: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        run_stdlib_graph_json(_canonical_json(graph), _canonical_json(inputs)),
        "native stdlib runtime result",
    )


def run_test_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    node_outputs: dict[str, object],
) -> dict[str, object]:
    return _json_object_result(
        run_test_graph_json(
            _canonical_json(graph),
            _canonical_json(inputs),
            _canonical_json(node_outputs),
        ),
        "native test runtime result",
    )


def finalize_tool_call(
    draft: dict[str, object],
    *,
    resolved_tool_id: str,
    created_at_unix_ms: int,
) -> dict[str, object]:
    return _json_object_result(
        finalize_tool_call_json(_canonical_json(draft), resolved_tool_id, created_at_unix_ms),
        "native finalized tool call",
    )


def prepare_tool_result_for_model(
    call: dict[str, object],
    result: dict[str, object],
    resolved_tool: dict[str, object],
    schema_registry: object,
    *,
    content_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    content_policy_json = None if content_policy is None else _canonical_json(content_policy)
    return _json_object_result(
        prepare_tool_result_for_model_json(
            _canonical_json(call),
            _canonical_json(result),
            _canonical_json(resolved_tool),
            _canonical_json(schema_registry),
            content_policy_json,
        ),
        "native prepared tool result",
    )


def record_tool_effect_precondition(
    resolved_tool: dict[str, object],
    call: dict[str, object],
    *,
    effect_key: str | None = None,
    idempotency_key: str | None = None,
    policy_decision_id: str | None = None,
    execution_target: str | None = None,
    sandbox_id: str | None = None,
) -> dict[str, object]:
    return _json_object_result(
        record_tool_effect_precondition_json(
            _canonical_json(resolved_tool),
            _canonical_json(call),
            effect_key,
            idempotency_key,
            policy_decision_id,
            execution_target,
            sandbox_id,
        ),
        "native tool effect precondition",
    )


def record_tool_effect_audit_event(
    *,
    event_id: str,
    occurred_at: str,
    actor: dict[str, object],
    resolved_tool: dict[str, object],
    call: dict[str, object],
    result: dict[str, object],
    effect_key: str | None = None,
    precondition_digest: str | None = None,
    idempotency_key: str | None = None,
    policy_decision_id: str | None = None,
) -> dict[str, object]:
    return _json_object_result(
        record_tool_effect_audit_event_json(
            event_id,
            occurred_at,
            _canonical_json(actor),
            _canonical_json(resolved_tool),
            _canonical_json(call),
            _canonical_json(result),
            effect_key,
            precondition_digest,
            idempotency_key,
            policy_decision_id,
        ),
        "native tool effect audit event",
    )


def decide_agent_step(spec: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        decide_agent_step_json(_canonical_json(spec), _canonical_json(request)),
        "native agent step decision",
    )


def admit_exhaustion_work(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        admit_exhaustion_work_json(_canonical_json(policy), _canonical_json(request)),
        "native exhaustion admission result",
    )


def admit_worker_message(
    message: dict[str, object],
    *,
    daemon_config: dict[str, object] | None = None,
    response_message_id: str = "message-daemon-1",
    response_sequence: int = 1,
) -> dict[str, object]:
    daemon_config_json = None if daemon_config is None else _canonical_json(daemon_config)
    return _json_object_result(
        admit_worker_message_json(
            _canonical_json(message),
            daemon_config_json,
            response_message_id,
            response_sequence,
        ),
        "native worker admission result",
    )


def evaluate_output_gate(
    gate: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_output_gate_json(_canonical_json(gate), _canonical_json(operations)),
        "native output gate result",
    )


def evaluate_declarative_output_policy(
    rules: object,
    chunk: dict[str, object],
    *,
    evaluated_at_unix_ms: int,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_declarative_output_policy_json(
            _canonical_json(rules),
            _canonical_json(chunk),
            evaluated_at_unix_ms,
        ),
        "native output policy decision",
    )


def evaluate_application_event_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_application_event_stream_json(_canonical_json(state), _canonical_json(operations)),
        "native application event stream result",
    )


def evaluate_application_protocol_log(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_application_protocol_log_json(_canonical_json(state), _canonical_json(operations)),
        "native application protocol log result",
    )


def evaluate_application_protocol_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_application_protocol_stream_json(_canonical_json(state), _canonical_json(operations)),
        "native application protocol stream result",
    )


def evaluate_connector_capabilities(
    connection: dict[str, object],
    required_capabilities: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_connector_capabilities_json(
            _canonical_json(connection),
            _canonical_json(required_capabilities),
        ),
        "native connector capability evaluation result",
    )


def negotiate_application_protocol_capabilities(
    server: dict[str, object],
    client: dict[str, object],
) -> dict[str, object]:
    return _json_object_result(
        negotiate_application_protocol_capabilities_json(
            _canonical_json(server),
            _canonical_json(client),
        ),
        "native application protocol capability negotiation result",
    )


def evaluate_durable_tool_terminal_store(operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_durable_tool_terminal_store_json(_canonical_json(operations)),
        "native durable tool terminal store result",
    )


def evaluate_tool_execution_plan(
    plan: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_tool_execution_plan_json(_canonical_json(plan), _canonical_json(operations)),
        "native tool execution plan result",
    )


def evaluate_tool_result_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_tool_result_stream_json(_canonical_json(state), _canonical_json(operations)),
        "native tool result stream result",
    )


def evaluate_sequential_tool_queue(
    queue: dict[str, object],
    operations: object,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_sequential_tool_queue_json(_canonical_json(queue), _canonical_json(operations)),
        "native sequential tool queue result",
    )


def evaluate_usage_ledger(operations: object, *, run_id: str | None = None) -> dict[str, object]:
    return _json_object_result(
        evaluate_usage_ledger_json(_canonical_json(operations), run_id),
        "native usage ledger result",
    )


def validate_worker_advertisement(
    advertisement: dict[str, object],
    *,
    expected_package_lock_hash: str | None = None,
) -> dict[str, object]:
    return _json_object_result(
        validate_worker_advertisement_json(_canonical_json(advertisement), expected_package_lock_hash),
        "native worker advertisement validation result",
    )


def validate_worker_protocol_message(message: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        validate_worker_protocol_message_json(_canonical_json(message)),
        "native worker protocol message validation result",
    )


def validate_remote_payload(payload: dict[str, object], *, max_inline_bytes: int) -> dict[str, object]:
    return _json_object_result(
        validate_remote_payload_json(_canonical_json(payload), max_inline_bytes),
        "native remote payload validation result",
    )


__all__ = [
    "__version__",
    "admit_exhaustion_work",
    "admit_exhaustion_work_json",
    "admit_worker_message",
    "admit_worker_message_json",
    "binding_version",
    "capture_telemetry_content",
    "capture_telemetry_content_json",
    "compile_graph",
    "compile_graph_json",
    "decide_agent_step",
    "decide_agent_step_json",
    "evaluate_declarative_output_policy",
    "evaluate_declarative_output_policy_json",
    "evaluate_application_event_stream",
    "evaluate_application_event_stream_json",
    "evaluate_application_protocol_log",
    "evaluate_application_protocol_log_json",
    "evaluate_application_protocol_stream",
    "evaluate_application_protocol_stream_json",
    "evaluate_connector_capabilities",
    "evaluate_connector_capabilities_json",
    "evaluate_durable_tool_terminal_store",
    "evaluate_durable_tool_terminal_store_json",
    "evaluate_output_gate",
    "evaluate_output_gate_json",
    "evaluate_sequential_tool_queue",
    "evaluate_sequential_tool_queue_json",
    "evaluate_tool_execution_plan",
    "evaluate_tool_execution_plan_json",
    "evaluate_tool_result_stream",
    "evaluate_tool_result_stream_json",
    "evaluate_usage_ledger",
    "evaluate_usage_ledger_json",
    "finalize_tool_call",
    "finalize_tool_call_json",
    "native_extension_available",
    "native_extension_status",
    "negotiate_application_protocol_capabilities",
    "negotiate_application_protocol_capabilities_json",
    "prepare_tool_result_for_model",
    "prepare_tool_result_for_model_json",
    "record_tool_effect_audit_event",
    "record_tool_effect_audit_event_json",
    "record_tool_effect_precondition",
    "record_tool_effect_precondition_json",
    "require_native_extension",
    "run_stdlib_graph",
    "run_stdlib_graph_json",
    "run_test_graph",
    "run_test_graph_json",
    "validate_remote_payload",
    "validate_remote_payload_json",
    "validate_worker_advertisement",
    "validate_worker_advertisement_json",
    "validate_worker_protocol_message",
    "validate_worker_protocol_message_json",
]
