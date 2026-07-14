from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import json
import math

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
        evaluate_budget_ledger_json,
        evaluate_cancellation_scope_json,
        evaluate_connector_capabilities_json,
        evaluate_declarative_output_policy_json,
        evaluate_durable_tool_terminal_store_json,
        evaluate_node_lifecycle_json,
        evaluate_output_gate_json,
        evaluate_provider_limit_policy_json,
        evaluate_readiness_json,
        evaluate_retry_policy_json,
        evaluate_scheduler_json,
        evaluate_sequential_tool_queue_json,
        evaluate_task_group_json,
        evaluate_timeout_deadline_json,
        evaluate_tool_admission_json,
        evaluate_tool_approval_json,
        evaluate_tool_execution_plan_json,
        evaluate_tool_resolution_json,
        evaluate_tool_result_stream_json,
        evaluate_usage_ledger_json,
        finalize_tool_call_json,
        negotiate_application_protocol_capabilities_json,
        prepare_tool_result_for_model_json,
        record_tool_effect_audit_event_json,
        record_tool_effect_precondition_json,
        run_stdlib_graph_json,
        run_stdlib_graph_with_options_json,
        run_test_graph_json,
        run_test_graph_with_options_json,
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

    def compile_graph_json(
        document_json: str,
        block_catalog_json: str | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> str:
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

    def evaluate_retry_policy_json(policy_json: str, request_json: str) -> str:
        require_native_extension()

    def evaluate_provider_limit_policy_json(policy_json: str, incident_json: str) -> str:
        require_native_extension()

    def evaluate_timeout_deadline_json(policy_json: str, request_json: str) -> str:
        require_native_extension()

    def evaluate_readiness_json(signals_json: str, dependencies_json: str) -> str:
        require_native_extension()

    def evaluate_scheduler_json(nodes_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_cancellation_scope_json(root_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_task_group_json(group_json: str, operations_json: str) -> str:
        require_native_extension()

    def evaluate_node_lifecycle_json(state_json: str, operations_json: str) -> str:
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

    def evaluate_tool_approval_json(
        record_json: str,
        resolved_tool_json: str,
        call_json: str,
        principal_id: str,
        now_unix_ms: int,
    ) -> str:
        require_native_extension()

    def evaluate_tool_admission_json(request_json: str) -> str:
        require_native_extension()

    def evaluate_tool_resolution_json(
        catalog_json: str,
        scope_json: str,
        effective_policy_snapshot_id: str,
    ) -> str:
        require_native_extension()

    def evaluate_usage_ledger_json(operations_json: str, run_id: str | None = None) -> str:
        require_native_extension()

    def evaluate_budget_ledger_json(operations_json: str) -> str:
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

    def run_test_graph_with_options_json(
        graph_json: str,
        inputs_json: str,
        node_outputs_json: str,
        options_json: str,
    ) -> str:
        require_native_extension()

    def run_stdlib_graph_json(graph_json: str, inputs_json: str) -> str:
        require_native_extension()

    def run_stdlib_graph_with_options_json(
        graph_json: str,
        inputs_json: str,
        options_json: str,
    ) -> str:
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
    pending = [value]
    occupied_strings: set[str] = set()
    has_decimal = False
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise TypeError("native JSON object keys must be strings")
                occupied_strings.add(key)
                pending.append(child)
        elif isinstance(current, list | tuple):
            pending.extend(current)
        elif isinstance(current, str):
            occupied_strings.add(current)
        elif isinstance(current, Decimal):
            has_decimal = True

    if not has_decimal:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    root: list[object] = [None]
    copies: list[tuple[object, dict[str, object] | list[object], str | int]] = [
        (value, root, 0)
    ]
    decimal_tokens: dict[str, str] = {}
    token_index = 0
    while copies:
        current, parent, key = copies.pop()
        if isinstance(current, Decimal):
            if not current.is_finite():
                raise ValueError("native JSON values must contain only finite numbers")
            number = current.as_tuple()
            digits = "".join(str(digit) for digit in number.digits)
            significant = digits.lstrip("0")
            if not significant:
                rendered = "-0.0" if number.sign else "0.0"
            else:
                exponent = int(number.exponent) + len(significant) - 1
                coefficient = significant.rstrip("0")
                sign = "-" if number.sign else ""
                if -4 <= exponent < 16:
                    point = exponent + 1
                    if point <= 0:
                        rendered = sign + "0." + ("0" * -point) + coefficient
                    elif point >= len(coefficient):
                        rendered = sign + coefficient + ("0" * (point - len(coefficient))) + ".0"
                    else:
                        rendered = sign + coefficient[:point] + "." + coefficient[point:]
                else:
                    rendered = sign + coefficient[0]
                    if len(coefficient) > 1:
                        rendered += "." + coefficient[1:]
                    rendered += f"e{'-' if exponent < 0 else '+'}{abs(exponent):02d}"
            token = f"\x00graphblocks-native-decimal-{token_index}\x00"
            while token in occupied_strings:
                token_index += 1
                token = f"\x00graphblocks-native-decimal-{token_index}\x00"
            token_index += 1
            occupied_strings.add(token)
            decimal_tokens[token] = rendered
            parent[key] = token
        elif isinstance(current, Mapping):
            copied: dict[str, object] = {}
            parent[key] = copied
            for child_key, child in reversed(tuple(current.items())):
                copies.append((child, copied, child_key))
        elif isinstance(current, list | tuple):
            copied_list: list[object] = [None] * len(current)
            parent[key] = copied_list
            for index in range(len(current) - 1, -1, -1):
                copies.append((current[index], copied_list, index))
        else:
            parent[key] = current

    encoded = json.dumps(
        root[0],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for token, rendered in decimal_tokens.items():
        encoded = encoded.replace(
            json.dumps(token, ensure_ascii=False, separators=(",", ":")),
            rendered,
        )
    return encoded


def _json_object_result(result_json: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(
            result_json,
            parse_float=lambda token: (
                float(token)
                if math.isfinite(float(token))
                and Decimal(str(float(token))) == Decimal(token)
                else Decimal(token)
            ),
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except ValueError as error:
        raise ValueError(f"{label} must be valid strict JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return payload


def capture_telemetry_content(decision: dict[str, object], content: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        capture_telemetry_content_json(_canonical_json(decision), _canonical_json(content)),
        "native telemetry captured content",
    )


def compile_graph(
    document: dict[str, object],
    block_catalog: object | None = None,
    *,
    allow_unknown_blocks: bool = False,
) -> dict[str, object]:
    if not isinstance(allow_unknown_blocks, bool):
        raise TypeError("allow_unknown_blocks must be a boolean")
    block_catalog_json = None if block_catalog is None else _canonical_json(block_catalog)
    return _json_object_result(
        compile_graph_json(
            _canonical_json(document),
            block_catalog_json,
            allow_unknown_blocks=allow_unknown_blocks,
        ),
        "native compiler result",
    )


def run_stdlib_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    *,
    run_id: str | None = None,
    run_store_path: str | None = None,
    journal_store_path: str | None = None,
    checkpoint_store_path: str | None = None,
    async_operation_store_path: str | None = None,
    callback_receipt: dict[str, object] | None = None,
    callback_admission_hmac_key: str | None = None,
    deployment_provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    if (
        run_id is not None
        or run_store_path is not None
        or journal_store_path is not None
        or checkpoint_store_path is not None
        or async_operation_store_path is not None
        or callback_receipt is not None
        or callback_admission_hmac_key is not None
        or deployment_provenance is not None
    ):
        options: dict[str, object] = {}
        if run_id is not None:
            options["runId"] = run_id
        if run_store_path is not None:
            options["runStorePath"] = run_store_path
        if journal_store_path is not None:
            options["journalStorePath"] = journal_store_path
        if checkpoint_store_path is not None:
            options["checkpointStorePath"] = checkpoint_store_path
        if async_operation_store_path is not None:
            options["asyncOperationStorePath"] = async_operation_store_path
        if callback_receipt is not None:
            options["callbackReceipt"] = callback_receipt
        if callback_admission_hmac_key is not None:
            options["callbackAdmissionHmacKey"] = callback_admission_hmac_key
        if deployment_provenance is not None:
            options["deploymentProvenance"] = deployment_provenance
        return _json_object_result(
            run_stdlib_graph_with_options_json(
                _canonical_json(graph),
                _canonical_json(inputs),
                _canonical_json(options),
            ),
            "native stdlib runtime result",
        )
    return _json_object_result(
        run_stdlib_graph_json(_canonical_json(graph), _canonical_json(inputs)),
        "native stdlib runtime result",
    )


def run_test_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    node_outputs: dict[str, object],
    *,
    run_id: str | None = None,
    run_store_path: str | None = None,
    journal_store_path: str | None = None,
    deployment_provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    if (
        run_id is not None
        or run_store_path is not None
        or journal_store_path is not None
        or deployment_provenance is not None
    ):
        options: dict[str, object] = {}
        if run_id is not None:
            options["runId"] = run_id
        if run_store_path is not None:
            options["runStorePath"] = run_store_path
        if journal_store_path is not None:
            options["journalStorePath"] = journal_store_path
        if deployment_provenance is not None:
            options["deploymentProvenance"] = deployment_provenance
        return _json_object_result(
            run_test_graph_with_options_json(
                _canonical_json(graph),
                _canonical_json(inputs),
                _canonical_json(node_outputs),
                _canonical_json(options),
            ),
            "native test runtime result",
        )
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


def evaluate_retry_policy(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        evaluate_retry_policy_json(_canonical_json(policy), _canonical_json(request)),
        "native retry policy decision",
    )


def evaluate_provider_limit_policy(
    policy: dict[str, object],
    incident: dict[str, object],
) -> dict[str, object]:
    return _json_object_result(
        evaluate_provider_limit_policy_json(_canonical_json(policy), _canonical_json(incident)),
        "native provider limit policy decision",
    )


def evaluate_timeout_deadline(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        evaluate_timeout_deadline_json(_canonical_json(policy), _canonical_json(request)),
        "native timeout deadline evaluation result",
    )


def evaluate_readiness(signals: object, dependencies: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_readiness_json(_canonical_json(signals), _canonical_json(dependencies)),
        "native readiness evaluation result",
    )


def evaluate_scheduler(nodes: object, operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_scheduler_json(_canonical_json(nodes), _canonical_json(operations)),
        "native scheduler evaluation result",
    )


def evaluate_cancellation_scope(root: dict[str, object], operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_cancellation_scope_json(_canonical_json(root), _canonical_json(operations)),
        "native cancellation scope evaluation result",
    )


def evaluate_task_group(group: dict[str, object], operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_task_group_json(_canonical_json(group), _canonical_json(operations)),
        "native task group evaluation result",
    )


def evaluate_node_lifecycle(state: dict[str, object], operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_node_lifecycle_json(_canonical_json(state), _canonical_json(operations)),
        "native node lifecycle evaluation result",
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


def evaluate_tool_approval(
    record: dict[str, object],
    resolved_tool: dict[str, object],
    call: dict[str, object],
    *,
    principal_id: str,
    now_unix_ms: int,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_tool_approval_json(
            _canonical_json(record),
            _canonical_json(resolved_tool),
            _canonical_json(call),
            principal_id,
            now_unix_ms,
        ),
        "native tool approval evaluation result",
    )


def evaluate_tool_admission(request: dict[str, object]) -> dict[str, object]:
    return _json_object_result(
        evaluate_tool_admission_json(_canonical_json(request)),
        "native tool admission evaluation result",
    )


def evaluate_tool_resolution(
    catalog: dict[str, object],
    scope: dict[str, object],
    *,
    effective_policy_snapshot_id: str,
) -> dict[str, object]:
    return _json_object_result(
        evaluate_tool_resolution_json(
            _canonical_json(catalog),
            _canonical_json(scope),
            effective_policy_snapshot_id,
        ),
        "native tool resolution evaluation result",
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


def evaluate_budget_ledger(operations: object) -> dict[str, object]:
    return _json_object_result(
        evaluate_budget_ledger_json(_canonical_json(operations)),
        "native budget ledger result",
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
    "evaluate_budget_ledger",
    "evaluate_budget_ledger_json",
    "evaluate_cancellation_scope",
    "evaluate_cancellation_scope_json",
    "evaluate_connector_capabilities",
    "evaluate_connector_capabilities_json",
    "evaluate_durable_tool_terminal_store",
    "evaluate_durable_tool_terminal_store_json",
    "evaluate_node_lifecycle",
    "evaluate_node_lifecycle_json",
    "evaluate_output_gate",
    "evaluate_output_gate_json",
    "evaluate_provider_limit_policy",
    "evaluate_provider_limit_policy_json",
    "evaluate_readiness",
    "evaluate_readiness_json",
    "evaluate_retry_policy",
    "evaluate_retry_policy_json",
    "evaluate_scheduler",
    "evaluate_scheduler_json",
    "evaluate_sequential_tool_queue",
    "evaluate_sequential_tool_queue_json",
    "evaluate_task_group",
    "evaluate_task_group_json",
    "evaluate_timeout_deadline",
    "evaluate_timeout_deadline_json",
    "evaluate_tool_admission",
    "evaluate_tool_admission_json",
    "evaluate_tool_approval",
    "evaluate_tool_approval_json",
    "evaluate_tool_execution_plan",
    "evaluate_tool_execution_plan_json",
    "evaluate_tool_resolution",
    "evaluate_tool_resolution_json",
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
    "run_stdlib_graph_with_options_json",
    "run_test_graph",
    "run_test_graph_json",
    "run_test_graph_with_options_json",
    "validate_remote_payload",
    "validate_remote_payload_json",
    "validate_worker_advertisement",
    "validate_worker_advertisement_json",
    "validate_worker_protocol_message",
    "validate_worker_protocol_message_json",
]
