from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json

from graphblocks import (
    AdmittedToolCall,
    ArtifactRef,
    ContentPart,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    ToolResult,
    ToolResultEvent,
    ToolResultValidationError,
    ToolSchemaRegistry,
    ToolSchemaValidationError,
    canonical_dumps,
    canonical_hash,
    validate_tool_result_for_model,
)


class OpenApiToolAdapterError(RuntimeError):
    pass


def evaluate_native_connector_capabilities(
    connection: Mapping[str, object],
    required_capabilities: object,
) -> dict[str, object]:
    from graphblocks.runtime import evaluate_connector_capabilities

    return evaluate_connector_capabilities(dict(connection), required_capabilities)


@dataclass(frozen=True, slots=True)
class OpenApiOperationInvocation:
    binding_id: str
    resolved_tool_id: str
    tool_name: str
    tool_call_id: str
    connection: str
    operation_id: str
    arguments_json: str
    arguments_digest: str
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "binding_id",
            "resolved_tool_id",
            "tool_name",
            "tool_call_id",
            "connection",
            "operation_id",
            "arguments_digest",
            "definition_digest",
            "binding_digest",
            "effective_policy_snapshot_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise OpenApiToolAdapterError(f"OpenAPI invocation {field_name} must not be empty")
            object.__setattr__(self, field_name, value.strip())
        if self.idempotency_key is not None:
            if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
                raise OpenApiToolAdapterError("OpenAPI invocation idempotency_key must not be empty")
            object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        if not isinstance(self.arguments_json, str) or not self.arguments_json.strip():
            raise OpenApiToolAdapterError("OpenAPI invocation arguments_json must not be empty")
        arguments = _openapi_invocation_arguments(self.arguments_json)
        try:
            actual_digest = canonical_hash(arguments)
            canonical_arguments = canonical_dumps(arguments)
        except (TypeError, ValueError) as error:
            raise OpenApiToolAdapterError("OpenAPI invocation arguments_json must be canonical JSON") from error
        if actual_digest != self.arguments_digest:
            raise OpenApiToolAdapterError("OpenAPI invocation arguments digest does not match arguments_json")
        object.__setattr__(self, "arguments_json", canonical_arguments)

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "openapi",
            "binding_id": self.binding_id,
            "resolved_tool_id": self.resolved_tool_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "connection": self.connection,
            "operation_id": self.operation_id,
            "arguments": _openapi_invocation_arguments(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
            "idempotency_key": self.idempotency_key,
        }


def _openapi_invocation_arguments(arguments_json: str) -> dict[str, object]:
    try:
        arguments = json.loads(
            arguments_json,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI invocation arguments_json must be valid JSON") from error
    if not isinstance(arguments, Mapping):
        raise OpenApiToolAdapterError("OpenAPI invocation arguments_json must decode to an object")
    return dict(arguments)


def define_openapi_tool(
    *,
    name: str,
    description: str,
    input_schema: str,
    output_schema: str | None = None,
    tags: Iterable[str] = (),
    version: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        tags=frozenset(tags),
        version=version,
    )


def bind_openapi_operation(
    *,
    binding_id: str,
    tool_name: str,
    connection: str,
    operation_id: str,
    effects: Iterable[str] = ("network",),
    approval: str = "policy",
    idempotency: str = "optional",
    cancellation: str = "cooperative",
    result_mode: str = "value",
    timeout_ms: int | None = None,
    retry_policy_ref: str | None = None,
    policy_profile_ref: str | None = None,
    execution_class: str | None = None,
) -> ToolBinding:
    return ToolBinding(
        binding_id=binding_id,
        tool_name=tool_name,
        implementation=OpenApiToolImplementation(connection=connection, operation_id=operation_id),
        effects=frozenset(effects),
        approval=approval,
        idempotency=idempotency,
        cancellation=cancellation,
        result_mode=result_mode,
        timeout_ms=timeout_ms,
        retry_policy_ref=retry_policy_ref,
        policy_profile_ref=policy_profile_ref,
        execution_class=execution_class,
    )


def define_openapi_tools_from_spec(
    spec: Mapping[str, object],
    *,
    schema_prefix: str = "schemas/openapi",
    tool_name_prefix: str | None = None,
    tags: Iterable[str] = (),
    version: str | None = None,
) -> tuple[ToolDefinition, ...]:
    if not isinstance(spec, Mapping):
        raise OpenApiToolAdapterError("OpenAPI spec must be an object")
    raw_paths = spec.get("paths")
    if not isinstance(raw_paths, Mapping):
        raise OpenApiToolAdapterError("OpenAPI spec paths must be an object")
    if any(
        not isinstance(path, str)
        or not path
        or (not path.startswith("/") and not path.startswith("x-"))
        for path in raw_paths
    ):
        raise OpenApiToolAdapterError("OpenAPI spec path keys must be non-empty strings")

    discovered: list[ToolDefinition] = []
    seen: set[str] = set()
    generated_schema_owners: dict[str, str] = {}
    base_tags = _string_set(tags, owner="OpenAPI discovery tags")
    for path in sorted(raw_paths):
        if path.startswith("x-"):
            continue
        path_item = raw_paths[path]
        if not isinstance(path_item, Mapping):
            continue
        for method in _OPENAPI_HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, Mapping):
                raise OpenApiToolAdapterError(
                    f"OpenAPI operation {method.upper()} {path} must be an object"
                )
            operation_id = _required_string(
                operation,
                "operationId",
                owner=f"OpenAPI operation {method.upper()} {path}",
            )
            tool_name = (
                _optional_text(operation, "x-graphblocks-tool-name")
                or (f"{tool_name_prefix}.{operation_id}" if tool_name_prefix else operation_id)
            )
            if tool_name in seen:
                raise OpenApiToolAdapterError(f"OpenAPI spec produces duplicate tool {tool_name!r}")
            seen.add(tool_name)

            generated_input_schema = _generated_schema_ref(schema_prefix, operation_id, "input")
            generated_output_schema = _generated_schema_ref(schema_prefix, operation_id, "output")
            input_schema = _operation_schema_ref(
                operation.get("x-graphblocks-input-schema"),
                fallback=_request_schema_ref(operation, schema_prefix, operation_id),
                owner=f"OpenAPI operation {operation_id} input schema",
                schema_prefix=schema_prefix,
            )
            output_schema = _operation_schema_ref(
                operation.get("x-graphblocks-output-schema"),
                fallback=_response_schema_ref(operation, schema_prefix, operation_id),
                owner=f"OpenAPI operation {operation_id} output schema",
                schema_prefix=schema_prefix,
            )
            operation_owner = f"{method.upper()} {path} ({operation_id})"
            for schema_ref, generated_ref in (
                (input_schema, generated_input_schema),
                (output_schema, generated_output_schema),
            ):
                if schema_ref != generated_ref:
                    continue
                previous_owner = generated_schema_owners.get(generated_ref)
                if previous_owner is not None:
                    raise OpenApiToolAdapterError(
                        "OpenAPI generated schema reference collision "
                        f"{generated_ref!r} between {previous_owner} and {operation_owner}"
                    )
                generated_schema_owners[generated_ref] = operation_owner
            discovered.append(
                define_openapi_tool(
                    name=tool_name,
                    description=(
                        _optional_text(operation, "description")
                        or _optional_text(operation, "summary")
                        or f"OpenAPI operation {operation_id}."
                    ),
                    input_schema=input_schema,
                    output_schema=output_schema,
                    tags=base_tags
                    | _string_set(
                        operation.get("tags", ()),
                        owner=f"OpenAPI operation {operation_id} tags",
                    ),
                    version=version,
                )
            )

    return tuple(sorted(discovered, key=lambda definition: definition.name))


def prepare_openapi_operation_invocation(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    validation_time: str | None = None,
) -> OpenApiOperationInvocation:
    implementation = resolved_tool.binding.implementation
    if not isinstance(implementation, OpenApiToolImplementation):
        raise OpenApiToolAdapterError("OpenAPI tool invocation requires an OpenAPI tool binding")
    if admitted.call.status != "admitted":
        raise OpenApiToolAdapterError(f"tool call {admitted.call.tool_call_id} is not admitted")
    if admitted.call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise OpenApiToolAdapterError("tool call references a different resolved tool")
    if admitted.call.name != resolved_tool.definition.name:
        raise OpenApiToolAdapterError("tool call name does not match resolved tool")
    _validate_resolved_tool_capability(
        admitted,
        resolved_tool,
        validation_time=validation_time,
        owner="OpenAPI",
    )
    if resolved_tool.binding.idempotency == "required" and admitted.idempotency_key is None:
        raise OpenApiToolAdapterError(
            f"OpenAPI tool call {admitted.call.tool_call_id} requires an idempotency key before execution"
        )
    try:
        actual_arguments_digest = canonical_hash(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise OpenApiToolAdapterError("tool arguments must be canonical JSON") from error
    if actual_arguments_digest != admitted.call.arguments_digest:
        raise OpenApiToolAdapterError("tool arguments digest does not match arguments")

    try:
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise OpenApiToolAdapterError("tool arguments must be canonical JSON") from error

    return OpenApiOperationInvocation(
        binding_id=resolved_tool.binding.binding_id,
        resolved_tool_id=resolved_tool.resolved_tool_id,
        tool_name=resolved_tool.definition.name,
        tool_call_id=admitted.call.tool_call_id,
        connection=implementation.connection,
        operation_id=implementation.operation_id,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        definition_digest=resolved_tool.definition_digest,
        binding_digest=resolved_tool.binding_digest,
        effective_policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
        idempotency_key=admitted.idempotency_key,
    )


def openapi_tool_result_from_response(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    output: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(output, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation response output must be an object")

    try:
        result = ToolResult.completed(
            admitted.call.tool_call_id,
            (
                ContentPart(
                    kind="json",
                    data=dict(output),
                    metadata={"adapter": "openapi", "trust_designation": "untrusted_external"},
                ),
            ),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error

    prepare_openapi_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    return result


def openapi_tool_result_started(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    started_at: str,
) -> ToolResultEvent:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    return ToolResultEvent.started(admitted.call.tool_call_id, sequence, started_at=started_at)


def openapi_tool_result_delta(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    output: Iterable[ContentPart | Mapping[str, object] | str],
) -> ToolResultEvent:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    return ToolResultEvent.delta(
        admitted.call.tool_call_id,
        sequence,
        _stream_content_parts(output, owner="OpenAPI"),
    )


def openapi_tool_result_artifact_ready(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    artifact: ArtifactRef | Mapping[str, object],
) -> ToolResultEvent:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    return ToolResultEvent.artifact_ready(
        admitted.call.tool_call_id,
        sequence,
        _artifact_ref(artifact, owner="OpenAPI"),
    )


def openapi_tool_result_completed(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    sequence: int,
    result: ToolResult,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResultEvent:
    if result.status != "completed":
        raise OpenApiToolAdapterError("OpenAPI completed event requires a completed tool result")
    prepare_openapi_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    try:
        return ToolResultEvent.completed(admitted.call.tool_call_id, sequence, result)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI completed event is invalid") from error


def openapi_tool_result_terminal_event(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    sequence: int,
    result: ToolResult,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResultEvent:
    if result.status == "completed":
        return openapi_tool_result_completed(
            admitted,
            resolved_tool,
            schema_registry,
            sequence=sequence,
            result=result,
            max_output_bytes=max_output_bytes,
            redactions=redactions,
            capture_policy=capture_policy,
        )

    prepare_openapi_operation_invocation(admitted, resolved_tool)
    constructors = {
        "failed": ToolResultEvent.failed,
        "denied": ToolResultEvent.denied,
        "cancelled": ToolResultEvent.cancelled,
        "policy_stopped": ToolResultEvent.policy_stopped,
        "incomplete": ToolResultEvent.incomplete,
    }
    constructor = constructors.get(result.status)
    if constructor is None:
        raise OpenApiToolAdapterError(
            f"OpenAPI terminal event does not support result status {result.status}"
        )
    try:
        return constructor(admitted.call.tool_call_id, sequence, result)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI terminal event is invalid") from error


def prepare_openapi_tool_result_for_model(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    result: ToolResult,
    *,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> tuple[ContentPart, ...]:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    try:
        return validate_tool_result_for_model(
            admitted.call,
            result,
            resolved_tool,
            schema_registry,
            max_output_bytes=max_output_bytes,
            redactions=tuple(dict(redaction) for redaction in redactions),
            capture_policy=dict(capture_policy) if capture_policy is not None else None,
        )
    except (ToolResultValidationError, ToolSchemaValidationError) as error:
        raise OpenApiToolAdapterError("OpenAPI tool result failed validation") from error


def openapi_tool_result_from_error(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation error must be an object")

    try:
        return ToolResult.failed(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_denied(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    completed_at: str,
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation denial error must be an object")
    return ToolResult.denied(
        admitted.call.tool_call_id,
        error=dict(error),
        completed_at=completed_at,
    )


def openapi_tool_result_policy_stopped(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation policy stop error must be an object")

    try:
        return ToolResult.policy_stopped(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_cancelled(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    try:
        return ToolResult.cancelled(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_incomplete(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    try:
        return ToolResult.incomplete(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def _stream_content_parts(
    output: Iterable[ContentPart | Mapping[str, object] | str],
    *,
    owner: str,
) -> tuple[ContentPart, ...]:
    if isinstance(output, str):
        raise OpenApiToolAdapterError(f"{owner} tool result delta output must be a sequence")
    try:
        raw_parts = tuple(output)
    except TypeError as error:
        raise OpenApiToolAdapterError(f"{owner} tool result delta output must be a sequence") from error
    parts: list[ContentPart] = []
    for raw_part in raw_parts:
        if isinstance(raw_part, ContentPart):
            metadata = dict(raw_part.metadata)
            metadata["adapter"] = "openapi"
            metadata["trust_designation"] = "untrusted_external"
            parts.append(replace(raw_part, metadata=metadata))
        elif isinstance(raw_part, str):
            parts.append(
                ContentPart(
                    kind="text",
                    text=raw_part,
                    metadata={"adapter": "openapi", "trust_designation": "untrusted_external"},
                )
            )
        elif isinstance(raw_part, Mapping):
            parts.append(_content_part(raw_part, owner=owner))
        else:
            raise OpenApiToolAdapterError(f"{owner} tool result delta output entries must be content parts")
    return tuple(parts)


def _content_part(raw_part: Mapping[str, object], *, owner: str) -> ContentPart:
    kind = raw_part.get("kind")
    metadata = raw_part.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise OpenApiToolAdapterError(f"{owner} tool result delta metadata must be an object")
    metadata = dict(metadata)
    metadata["adapter"] = owner.lower()
    metadata["trust_designation"] = "untrusted_external"
    if kind is None:
        kind = "text" if "text" in raw_part else "json" if "data" in raw_part else None
    if kind == "text":
        text = raw_part.get("text")
        if not isinstance(text, str):
            raise OpenApiToolAdapterError(f"{owner} text delta output requires string text")
        return ContentPart(kind="text", text=text, metadata=metadata)
    if kind in {"json", "artifact_ref"}:
        data = raw_part.get("data")
        if not isinstance(data, Mapping):
            raise OpenApiToolAdapterError(f"{owner} {kind} delta output requires object data")
        return ContentPart(kind=kind, data=dict(data), metadata=metadata)  # type: ignore[arg-type]
    raise OpenApiToolAdapterError(f"{owner} tool result delta has unknown content kind {kind!r}")


def _artifact_ref(artifact: ArtifactRef | Mapping[str, object], *, owner: str) -> ArtifactRef:
    if isinstance(artifact, ArtifactRef):
        return artifact
    if not isinstance(artifact, Mapping):
        raise OpenApiToolAdapterError(f"{owner} tool result artifact must be an ArtifactRef or object")
    artifact_id = artifact.get("artifact_id", artifact.get("artifactId"))
    uri = artifact.get("uri")
    if not isinstance(artifact_id, str) or not isinstance(uri, str):
        raise OpenApiToolAdapterError(f"{owner} tool result artifact requires artifact_id and uri")
    metadata = artifact.get("metadata", {})
    if not isinstance(metadata, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
    ):
        raise OpenApiToolAdapterError(f"{owner} tool result artifact metadata must be a string object")
    try:
        return ArtifactRef(
            artifact_id=artifact_id,
            uri=uri,
            media_type=_optional_string(artifact, "media_type", "mediaType", owner=owner),
            size_bytes=_optional_integer(artifact, "size_bytes", "sizeBytes", owner=owner),
            checksum=_optional_string(artifact, "checksum", owner=owner),
            etag=_optional_string(artifact, "etag", owner=owner),
            version=_optional_string(artifact, "version", owner=owner),
            filename=_optional_string(artifact, "filename", owner=owner),
            metadata=dict(metadata),
        )
    except ValueError as error:
        raise OpenApiToolAdapterError(f"{owner} tool result artifact is invalid") from error


def _optional_string(artifact: Mapping[str, object], *names: str, owner: str) -> str | None:
    for name in names:
        if name in artifact:
            value = artifact[name]
            if value is None or isinstance(value, str):
                return value
            raise OpenApiToolAdapterError(f"{owner} tool result artifact {name} must be a string")
    return None


def _optional_integer(artifact: Mapping[str, object], *names: str, owner: str) -> int | None:
    for name in names:
        if name in artifact:
            value = artifact[name]
            if value is None:
                return None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise OpenApiToolAdapterError(f"{owner} tool result artifact {name} must be an integer")
    return None


_OPENAPI_HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")


def _request_schema_ref(operation: Mapping[str, object], schema_prefix: str, operation_id: str) -> str:
    request_body = operation.get("requestBody")
    if isinstance(request_body, Mapping):
        schema = _json_content_schema(request_body)
        if schema is not None:
            return _schema_ref_from_openapi_schema(
                schema,
                fallback=_generated_schema_ref(schema_prefix, operation_id, "input"),
                schema_prefix=schema_prefix,
            )
    return _generated_schema_ref(schema_prefix, operation_id, "input")


def _response_schema_ref(
    operation: Mapping[str, object],
    schema_prefix: str,
    operation_id: str,
) -> str | None:
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return None
    normalized_status_codes: set[str] = set()
    for status_code in responses:
        normalized_status_code = str(status_code)
        if normalized_status_code in normalized_status_codes:
            raise OpenApiToolAdapterError(
                f"OpenAPI operation {operation_id!r} has duplicate response status code "
                f"{normalized_status_code!r} after normalization"
            )
        normalized_status_codes.add(normalized_status_code)
    for status_code in sorted(responses, key=str):
        if not _is_success_status_code(str(status_code)):
            continue
        response = responses[status_code]
        if not isinstance(response, Mapping):
            continue
        schema = _json_content_schema(response)
        if schema is not None:
            return _schema_ref_from_openapi_schema(
                schema,
                fallback=_generated_schema_ref(schema_prefix, operation_id, "output"),
                schema_prefix=schema_prefix,
            )
    return None


def _json_content_schema(container: Mapping[str, object]) -> Mapping[str, object] | None:
    content = container.get("content")
    if not isinstance(content, Mapping):
        return None
    candidates: list[tuple[tuple[int, int, str], Mapping[str, object]]] = []
    for raw_media_type, raw_media in content.items():
        if not isinstance(raw_media_type, str) or not isinstance(raw_media, Mapping):
            continue
        media_type = raw_media_type.strip().lower()
        base_media_type = media_type.split(";", 1)[0].strip()
        if base_media_type == "application/json":
            priority = (0, 0 if media_type == "application/json" else 1, media_type)
        elif base_media_type.startswith("application/") and base_media_type.endswith("+json"):
            priority = (1, 0 if ";" not in media_type else 1, media_type)
        else:
            continue
        candidates.append((priority, raw_media))
    for _priority, media in sorted(candidates, key=lambda candidate: candidate[0]):
        schema = media.get("schema")
        if isinstance(schema, Mapping):
            return schema
    return None


def _operation_schema_ref(
    value: object,
    *,
    fallback: str | None,
    owner: str,
    schema_prefix: str,
) -> str | None:
    if value is None:
        return fallback
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise OpenApiToolAdapterError(f"{owner} must not be empty")
        return value
    if isinstance(value, Mapping):
        return _schema_ref_from_openapi_schema(value, fallback=fallback, schema_prefix=schema_prefix)
    raise OpenApiToolAdapterError(f"{owner} must be a string or object")


def _schema_ref_from_openapi_schema(
    schema: Mapping[str, object],
    *,
    fallback: str | None,
    schema_prefix: str,
) -> str | None:
    for key in ("x-graphblocks-schema-ref", "schemaId", "schema_id", "$id"):
        schema_ref = schema.get(key)
        if isinstance(schema_ref, str) and schema_ref:
            return schema_ref
    schema_ref = schema.get("$ref")
    if isinstance(schema_ref, str) and schema_ref:
        prefix = schema_prefix.strip().rstrip("/")
        if not prefix:
            raise OpenApiToolAdapterError("OpenAPI schema_prefix must not be empty")
        return f"{prefix}/{_schema_slug(schema_ref.rsplit('/', 1)[-1])}@1"
    return fallback


def _is_success_status_code(status_code: str) -> bool:
    if status_code.lower() == "default":
        return False
    if status_code.upper() == "2XX":
        return True
    return len(status_code) == 3 and status_code[0] == "2" and status_code[1:].isdigit()


def _validate_resolved_tool_capability(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    validation_time: str | None,
    owner: str,
) -> None:
    if not resolved_tool.allowed_for_principal:
        raise OpenApiToolAdapterError(
            f"{owner} resolved tool {resolved_tool.definition.name} is not allowed for principal"
        )
    effective_time = validation_time if validation_time is not None else admitted.call.admitted_at
    if not isinstance(effective_time, str) or not effective_time.strip():
        raise OpenApiToolAdapterError(f"{owner} tool invocation validation_time must be a non-empty string")
    if resolved_tool.valid_until is not None:
        effective_datetime = _parse_iso_datetime(
            effective_time,
            owner=owner,
            field="validation_time",
        )
        valid_until_datetime = _parse_iso_datetime(
            resolved_tool.valid_until,
            owner=owner,
            field="valid_until",
        )
        if effective_datetime >= valid_until_datetime:
            raise OpenApiToolAdapterError(
                f"{owner} resolved tool {resolved_tool.definition.name} expired at {resolved_tool.valid_until}"
            )


def _parse_iso_datetime(value: str, *, owner: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OpenApiToolAdapterError(f"{owner} tool invocation {field} must be a non-empty ISO datetime")
    normalized = value.strip()
    if normalized != value or len(normalized) <= 10 or normalized[10] != "T":
        raise OpenApiToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime")
    suffix = normalized[19:]
    suffix_valid = False
    if suffix.startswith("."):
        offset_start = min(
            (
                position
                for position in (
                    suffix.find("Z"),
                    suffix.find("+"),
                    suffix.find("-"),
                )
                if position >= 0
            ),
            default=-1,
        )
        if offset_start > 1 and suffix[1:offset_start].isdigit():
            suffix = suffix[offset_start:]
    if suffix == "Z":
        suffix_valid = True
    elif (
        len(suffix) == 6
        and suffix[0] in "+-"
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
        and 0 <= int(suffix[1:3]) <= 23
        and 0 <= int(suffix[4:6]) <= 59
    ):
        suffix_valid = True
    if not suffix_valid:
        raise OpenApiToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise OpenApiToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_string(value: Mapping[str, object], name: str, *, owner: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise OpenApiToolAdapterError(f"{owner} requires non-empty string {name}")
    return item.strip()


def _optional_text(value: Mapping[str, object], name: str) -> str | None:
    item = value.get(name)
    if not isinstance(item, str):
        return None
    item = item.strip()
    return item or None


def _string_set(value: Iterable[str] | object, *, owner: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes, Mapping)):
        raise OpenApiToolAdapterError(f"{owner} must be a sequence of strings")
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise OpenApiToolAdapterError(f"{owner} must be a sequence of strings") from error
    stripped_values: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise OpenApiToolAdapterError(f"{owner} must contain only non-empty strings")
        stripped_values.append(item.strip())
    return frozenset(stripped_values)


def _generated_schema_ref(schema_prefix: str, operation_id: str, direction: str) -> str:
    prefix = schema_prefix.strip().rstrip("/")
    if not prefix:
        raise OpenApiToolAdapterError("OpenAPI schema_prefix must not be empty")
    return f"{prefix}/{_schema_slug(operation_id)}/{direction}@1"


def _schema_slug(value: str) -> str:
    parts: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            parts.append(char)
            previous_dash = False
        elif not previous_dash:
            parts.append("-")
            previous_dash = True
    slug = "".join(parts).strip("-")
    return slug or "operation"


__all__ = [
    "OpenApiOperationInvocation",
    "OpenApiToolAdapterError",
    "bind_openapi_operation",
    "define_openapi_tool",
    "define_openapi_tools_from_spec",
    "evaluate_native_connector_capabilities",
    "openapi_tool_result_artifact_ready",
    "openapi_tool_result_cancelled",
    "openapi_tool_result_completed",
    "openapi_tool_result_denied",
    "openapi_tool_result_delta",
    "openapi_tool_result_from_error",
    "openapi_tool_result_from_response",
    "openapi_tool_result_incomplete",
    "openapi_tool_result_policy_stopped",
    "openapi_tool_result_started",
    "openapi_tool_result_terminal_event",
    "prepare_openapi_operation_invocation",
    "prepare_openapi_tool_result_for_model",
]
