from __future__ import annotations

from dataclasses import replace
import importlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from graphblocks import (
    AdmittedToolCall,
    JsonSchema,
    JsonSchemaNode,
    RemoteToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolCall,
    ToolDefinition,
    ToolSchemaRegistry,
    canonical_hash,
)


ROOT = Path(__file__).parents[1]


def _remote_admitted_call(
    *,
    arguments: dict[str, object],
    idempotency: str = "not_applicable",
    idempotency_key: str | None = "idem-1",
    output_schema: str | None = None,
) -> tuple[AdmittedToolCall, ResolvedTool]:
    definition = ToolDefinition(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        output_schema=output_schema,
    )
    binding = ToolBinding(
        binding_id="binding-remote-search",
        tool_name="knowledge.search",
        implementation=RemoteToolImplementation(connection="support-api", operation="search"),
        effects=frozenset({"external_read", "network"}),
        approval="never",
        idempotency=idempotency,
    )
    resolved = ResolvedTool.from_definition_and_binding(
        resolved_tool_id="resolved-remote-search",
        definition=definition,
        binding=binding,
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
    )
    call = ToolCall(
        tool_call_id="call-1",
        response_id="response-1",
        resolved_tool_id=resolved.resolved_tool_id,
        name="knowledge.search",
        arguments=arguments,
        arguments_digest=canonical_hash(arguments),
        status="admitted",
        admitted_at="2026-06-24T00:00:00Z",
    )
    return AdmittedToolCall(call=call, idempotency_key=idempotency_key), resolved


def _remote_output_registry() -> ToolSchemaRegistry:
    return ToolSchemaRegistry(
        (
            JsonSchema(
                "schemas/SearchResult@1",
                JsonSchemaNode.object().required_property(
                    "items",
                    JsonSchemaNode.array(JsonSchemaNode.string()),
                ),
            ),
        )
    )


def test_client_package_exposes_application_event_protocol(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    metadata = graphblocks_client.ApplicationEventMetadata(
        event_id="event-1",
        run_id="run-1",
        response_id="response-1",
        turn_id="turn-1",
        sequence=1,
        release_id="release-1",
        policy_snapshot_id="policy-1",
        occurred_at="2026-06-23T00:00:00Z",
    )
    event = graphblocks_client.ApplicationEvent.new(
        "OutputPolicyAllowed",
        metadata,
        payload={"decision_id": "decision-1"},
    )
    cutoff = graphblocks_client.ApplicationEvent.new(
        "OutputCutoff",
        metadata,
        payload={
            "stream_id": "stream-1",
            "response_id": "response-1",
            "turn_id": "turn-1",
            "last_generated_sequence": 3,
            "last_policy_accepted_sequence": 1,
            "last_client_delivered_sequence": 1,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
            "durable_result": "none",
            "policy_decision_id": "decision-1",
            "occurred_at": "2026-06-23T00:00:01Z",
        },
    )
    late = graphblocks_client.ApplicationEvent.new(
        "OutputPolicyEvaluationStarted",
        metadata,
        payload={
            "stream_id": "stream-1",
            "response_id": "response-1",
            "chunk_sequence": 2,
            "input_digest": "sha256:late",
        },
    )
    state = graphblocks_client.ApplicationEventStreamState()

    assert event.kind == "OutputPolicyAllowed"
    assert event.metadata.run_id == "run-1"
    assert event.payload == {"decision_id": "decision-1"}
    assert state.accept(cutoff) == cutoff
    assert state.accept(late) is None
    assert "ToolCallCompleted" in graphblocks_client.TOOL_APPLICATION_EVENT_KINDS
    assert "OutputCutoff" in graphblocks_client.STANDARD_APPLICATION_EVENT_KINDS


def test_client_package_preserves_authoritative_event_metadata_from_payloads(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    events = graphblocks_client._application_events_from_payloads(
        [
            {
                "kind": "RunStarted",
                "metadata": {
                    "eventId": "event-1",
                    "runId": "run-1",
                    "responseId": "response-1",
                    "turnId": "turn-1",
                    "sequence": 7,
                    "cursor": "run-1:7",
                    "releaseId": "release-1",
                    "policySnapshotId": "policy-1",
                    "occurredAt": "2026-07-02T00:00:00Z",
                    "graphId": "graph-1",
                    "nodeId": "node-1",
                    "operationId": "operation-1",
                    "visibility": "operator",
                },
                "payload": {"status": "running"},
            }
        ]
    )

    assert events[0].metadata.cursor == "run-1:7"
    assert events[0].metadata.graph_id == "graph-1"
    assert events[0].metadata.node_id == "node-1"
    assert events[0].metadata.operation_id == "operation-1"
    assert events[0].metadata.visibility == "operator"

    with pytest.raises(ValueError, match="metadata visibility must be a valid visibility value"):
        graphblocks_client._application_events_from_payloads(
            [
                {
                    "kind": "RunStarted",
                    "metadata": {
                        "eventId": "event-2",
                        "runId": "run-1",
                        "responseId": "response-1",
                        "sequence": 8,
                        "releaseId": "release-1",
                        "policySnapshotId": "policy-1",
                        "occurredAt": "2026-07-02T00:00:01Z",
                        "visibility": "public",
                    },
                    "payload": {},
                }
            ]
        )


def test_client_package_exposes_application_protocol_envelopes(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    command = graphblocks_client.ApplicationCommand.new(
        "InvokeGraph",
        graphblocks_client.ApplicationCommandMetadata(
            command_id="command-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=1,
            issued_at_unix_ms=1_765_843_200_000,
        ),
        payload={"graph": "support-agent-turn"},
    )
    event = graphblocks_client.ApplicationProtocolEvent.new(
        "AssistantDraftDelta",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=2,
            cursor="cursor-2",
            occurred_at_unix_ms=1_765_843_201_000,
        ),
        payload={"delta": "hello"},
    )
    cutoff = graphblocks_client.ApplicationProtocolEvent.new(
        "OutputCutoff",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-2",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=3,
            cursor="cursor-3",
            occurred_at_unix_ms=1_765_843_202_000,
        ),
        payload={
            "response_id": "response-1",
            "last_client_delivered_sequence": 1,
            "terminal_reason": "policy_denied",
            "draft_disposition": "retract",
        },
    )
    late = graphblocks_client.ApplicationProtocolEvent.new(
        "AssistantDraftDelta",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-3",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=4,
            cursor="cursor-4",
            occurred_at_unix_ms=1_765_843_203_000,
        ),
        payload={"response_id": "response-1", "chunk_sequence": 2, "delta": "blocked"},
    )
    state = graphblocks_client.ApplicationProtocolStreamState()
    log = graphblocks_client.ApplicationProtocolLog()
    capabilities = graphblocks_client.ApplicationProtocolCapabilities(
        "graphblocks.app.v1"
    ).with_commands(["InvokeGraph", "CancelRun"])
    peer_capabilities = graphblocks_client.ApplicationProtocolCapabilities(
        "graphblocks.app.v1"
    ).with_commands(["CancelRun", "OpenArtifact"])

    assert command.payload == {"graph": "support-agent-turn"}
    assert event.kind == "AssistantDraftDelta"
    assert log.append(event) is True
    assert log.replay_after(limit=1) == (event,)
    assert capabilities.negotiate(peer_capabilities).commands == ("CancelRun",)
    assert state.accept(cutoff) == cutoff
    assert state.accept(late) is None
    assert "RequestSnapshot" in graphblocks_client.APPLICATION_COMMAND_KINDS
    assert "AssistantDraftDelta" in graphblocks_client.APPLICATION_PROTOCOL_EVENT_KINDS
    assert "ApplicationCommand" in graphblocks_client.__all__
    assert "ApplicationProtocolCapabilities" in graphblocks_client.__all__
    assert "ApplicationProtocolEvent" in graphblocks_client.__all__
    assert "ApplicationProtocolLog" in graphblocks_client.__all__
    assert "ApplicationProtocolStreamState" in graphblocks_client.__all__


def test_client_package_lazy_native_application_stream_helpers_delegate_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    calls: list[tuple[str, object, object]] = []

    def evaluate_application_event_stream(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("event_stream", state, operations))
        return {"kind": "event_stream", "state": state, "operations": operations}

    def evaluate_application_protocol_log(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("protocol_log", state, operations))
        return {"kind": "protocol_log", "state": state, "operations": operations}

    def evaluate_application_protocol_stream(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("protocol_stream", state, operations))
        return {"kind": "protocol_stream", "state": state, "operations": operations}

    def negotiate_application_protocol_capabilities(
        server: dict[str, object],
        client: dict[str, object],
    ) -> dict[str, object]:
        calls.append(("capabilities", server, client))
        return {"kind": "capabilities", "server": server, "client": client}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            evaluate_application_event_stream=evaluate_application_event_stream,
            evaluate_application_protocol_log=evaluate_application_protocol_log,
            evaluate_application_protocol_stream=evaluate_application_protocol_stream,
            negotiate_application_protocol_capabilities=negotiate_application_protocol_capabilities,
        ),
    )
    graphblocks_client = importlib.import_module("graphblocks_client")

    event_stream = graphblocks_client.evaluate_native_application_event_stream(
        {"acceptedThrough": 1},
        [{"op": "event", "sequence": 2}],
    )
    protocol_log = graphblocks_client.evaluate_native_application_protocol_log(
        {"lastSequence": 2},
        [{"op": "append", "sequence": 3}],
    )
    protocol_stream = graphblocks_client.evaluate_native_application_protocol_stream(
        {"lastClientDeliveredSequence": 3},
        [{"op": "event", "sequence": 4}],
    )
    capabilities = graphblocks_client.negotiate_native_application_protocol_capabilities(
        {"commands": ["InvokeGraph", "CancelRun"]},
        {"commands": ["CancelRun"]},
    )

    assert event_stream == {
        "kind": "event_stream",
        "state": {"acceptedThrough": 1},
        "operations": [{"op": "event", "sequence": 2}],
    }
    assert protocol_log == {
        "kind": "protocol_log",
        "state": {"lastSequence": 2},
        "operations": [{"op": "append", "sequence": 3}],
    }
    assert protocol_stream == {
        "kind": "protocol_stream",
        "state": {"lastClientDeliveredSequence": 3},
        "operations": [{"op": "event", "sequence": 4}],
    }
    assert capabilities == {
        "kind": "capabilities",
        "server": {"commands": ["InvokeGraph", "CancelRun"]},
        "client": {"commands": ["CancelRun"]},
    }
    assert calls == [
        ("event_stream", {"acceptedThrough": 1}, [{"op": "event", "sequence": 2}]),
        ("protocol_log", {"lastSequence": 2}, [{"op": "append", "sequence": 3}]),
        ("protocol_stream", {"lastClientDeliveredSequence": 3}, [{"op": "event", "sequence": 4}]),
        ("capabilities", {"commands": ["InvokeGraph", "CancelRun"]}, {"commands": ["CancelRun"]}),
    ]
    assert "evaluate_native_application_event_stream" in graphblocks_client.__all__
    assert "evaluate_native_application_protocol_log" in graphblocks_client.__all__
    assert "evaluate_native_application_protocol_stream" in graphblocks_client.__all__
    assert "negotiate_native_application_protocol_capabilities" in graphblocks_client.__all__


def test_client_package_builds_remote_tool_definition_binding_and_invocation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    arguments = {"query": "billing", "limit": 5}
    admitted, resolved = _remote_admitted_call(arguments=arguments)

    definition = graphblocks_client.define_remote_tool(
        name="knowledge.search",
        description="Search support documentation.",
        input_schema="schemas/SearchRequest@1",
        output_schema="schemas/SearchResult@1",
        tags=("support", "search"),
        version="1.0.0",
    )
    binding = graphblocks_client.bind_remote_tool(
        binding_id="binding-remote-search",
        tool_name="knowledge.search",
        connection="support-api",
        operation="search",
        effects=("external_read", "network"),
        approval="never",
        idempotency="not_applicable",
        timeout_ms=1_000,
        retry_policy_ref="retry/read",
        policy_profile_ref="policy/tool-output",
        execution_class="io-bound",
    )

    invocation = graphblocks_client.prepare_remote_tool_invocation(admitted, resolved)
    arguments["query"] = "mutated"

    assert definition.model_contract()["tags"] == ["search", "support"]
    assert binding.binding_contract()["implementation"] == {
        "kind": "remote",
        "connection": "support-api",
        "operation": "search",
    }
    assert binding.binding_contract()["timeout_ms"] == 1_000
    assert invocation.request_contract() == {
        "kind": "remote",
        "binding_id": "binding-remote-search",
        "resolved_tool_id": "resolved-remote-search",
        "tool_name": "knowledge.search",
        "tool_call_id": "call-1",
        "connection": "support-api",
        "operation": "search",
        "arguments": {"limit": 5, "query": "billing"},
        "arguments_digest": admitted.call.arguments_digest,
        "definition_digest": resolved.definition_digest,
        "binding_digest": resolved.binding_digest,
        "effective_policy_snapshot_id": "policy-snapshot-1",
        "idempotency_key": "idem-1",
    }
    direct_kwargs = {
        "binding_id": invocation.binding_id,
        "resolved_tool_id": invocation.resolved_tool_id,
        "tool_name": invocation.tool_name,
        "tool_call_id": invocation.tool_call_id,
        "connection": invocation.connection,
        "operation": invocation.operation,
        "arguments_json": '{"query":"billing","limit":5}',
        "arguments_digest": invocation.arguments_digest,
        "definition_digest": invocation.definition_digest,
        "binding_digest": invocation.binding_digest,
        "effective_policy_snapshot_id": invocation.effective_policy_snapshot_id,
        "idempotency_key": invocation.idempotency_key,
    }
    direct = graphblocks_client.RemoteToolInvocation(**direct_kwargs)

    assert direct.arguments_json == '{"limit":5,"query":"billing"}'
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="connection must not be empty"):
        graphblocks_client.RemoteToolInvocation(**{**direct_kwargs, "connection": " "})
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="must decode to an object"):
        graphblocks_client.RemoteToolInvocation(**{**direct_kwargs, "arguments_json": "[]"})
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="digest does not match"):
        graphblocks_client.RemoteToolInvocation(**{**direct_kwargs, "arguments_json": '{"query":"changed"}'})
    assert "prepare_remote_tool_invocation" in graphblocks_client.__all__


def test_client_package_remote_adapter_rejects_stale_argument_digest(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(arguments={"query": "billing"})
    object.__setattr__(admitted.call, "arguments", {"query": "mutated"})

    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="arguments digest does not match"):
        graphblocks_client.prepare_remote_tool_invocation(admitted, resolved)


def test_client_package_remote_adapter_rechecks_resolved_tool_capability(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(arguments={"query": "billing"})

    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="not allowed for principal"):
        graphblocks_client.prepare_remote_tool_invocation(
            admitted,
            replace(resolved, allowed_for_principal=False),
        )

    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="expired at 2026-06-24T00:00:01Z"):
        graphblocks_client.prepare_remote_tool_invocation(
            admitted,
            replace(resolved, valid_until="2026-06-24T00:00:01Z"),
            validation_time="2026-06-24T00:00:02Z",
        )

    offset_valid = replace(resolved, valid_until="2026-06-24T00:00:00-05:00")
    invocation = graphblocks_client.prepare_remote_tool_invocation(
        admitted,
        offset_valid,
        validation_time="2026-06-24T04:59:59Z",
    )
    assert invocation.tool_call_id == "call-1"
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="expired at 2026-06-24T00:00:00-05:00"):
        graphblocks_client.prepare_remote_tool_invocation(
            admitted,
            offset_valid,
            validation_time="2026-06-24T05:00:01Z",
        )


def test_client_package_remote_adapter_requires_required_idempotency_key(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(
        arguments={"query": "billing"},
        idempotency="required",
        idempotency_key=None,
    )

    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="requires an idempotency key"):
        graphblocks_client.prepare_remote_tool_invocation(admitted, resolved)


def test_client_package_remote_adapter_validates_result_before_model_return(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(
        arguments={"query": "billing"},
        output_schema="schemas/SearchResult@1",
    )
    registry = _remote_output_registry()

    result = graphblocks_client.remote_tool_result_from_response(
        admitted,
        resolved,
        registry,
        output={"items": ["refund policy"]},
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:02Z",
        effect_outcome="no_external_effect",
    )
    model_output = graphblocks_client.prepare_remote_tool_result_for_model(
        admitted,
        resolved,
        registry,
        result,
    )

    assert result.status == "completed"
    assert result.effect_outcome == "no_external_effect"
    assert result.output[0].metadata["adapter"] == "remote"
    assert model_output[0].metadata["trust_designation"] == "untrusted_external"
    assert model_output[0].metadata["prompt_injection_label"] == "untrusted_tool_output"
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="failed validation"):
        graphblocks_client.remote_tool_result_from_response(
            admitted,
            resolved,
            registry,
            output={"items": [7]},
            started_at="2026-06-24T00:00:01Z",
            completed_at="2026-06-24T00:00:02Z",
        )


def test_client_package_remote_adapter_builds_streaming_and_terminal_results(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(arguments={"query": "billing"})

    started = graphblocks_client.remote_tool_result_started(
        admitted,
        resolved,
        sequence=1,
        started_at="2026-06-24T00:00:01Z",
    )
    delta = graphblocks_client.remote_tool_result_delta(
        admitted,
        resolved,
        sequence=2,
        output=("partial", {"kind": "json", "data": {"items": ["draft"]}}),
    )
    artifact = graphblocks_client.remote_tool_result_artifact_ready(
        admitted,
        resolved,
        sequence=3,
        artifact={
            "artifactId": "artifact-1",
            "uri": "blob://artifact-1",
            "mediaType": "application/json",
            "metadata": {"source": "remote"},
        },
    )
    stopped = graphblocks_client.remote_tool_result_policy_stopped(
        admitted,
        resolved,
        error={"code": "policy.denied", "message": "stopped after remote response"},
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:03Z",
        effect_outcome="unknown",
    )
    incomplete = graphblocks_client.remote_tool_result_incomplete(
        admitted,
        resolved,
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:03Z",
    )
    completed = graphblocks_client.ToolResult.completed(
        "call-1",
        (graphblocks_client.ContentPart(kind="json", data={"items": ["billing"]}),),
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:03Z",
    )
    stream = graphblocks_client.ToolResultStreamState()
    stopped_event = graphblocks_client.remote_tool_result_terminal_event(
        admitted,
        resolved,
        _remote_output_registry(),
        sequence=4,
        result=stopped,
    )
    completed_event = graphblocks_client.remote_tool_result_completed(
        admitted,
        resolved,
        _remote_output_registry(),
        sequence=4,
        result=completed,
    )

    assert started.kind == "started"
    assert delta.output[0].metadata == {"adapter": "remote", "trust_designation": "untrusted_external"}
    assert delta.output[1].metadata == {"adapter": "remote", "trust_designation": "untrusted_external"}
    assert artifact.artifact is not None
    assert artifact.artifact.media_type == "application/json"
    assert stream.accept(started) == started
    assert stream.accept(delta).into_result() is None
    assert stream.accept(artifact).is_final_durable_result() is False
    assert stream.accept(stopped_event).into_result() == stopped
    with pytest.raises(graphblocks_client.ToolResultStreamError) as error:
        stream.accept(
            graphblocks_client.remote_tool_result_delta(
                admitted,
                resolved,
                sequence=5,
                output=("late",),
            )
        )
    assert error.value.final_status == "policy_stopped"
    assert stopped.status == "policy_stopped"
    assert stopped.effect_outcome == "unknown"
    assert incomplete.status == "incomplete"
    assert completed_event.kind == "completed"
    assert completed_event.into_result() == completed
    assert "ToolResultEvent" in graphblocks_client.__all__
    assert "ToolResultStreamState" in graphblocks_client.__all__
    assert "ToolResultStreamError" in graphblocks_client.__all__
    assert "remote_tool_result_completed" in graphblocks_client.__all__
    assert "remote_tool_result_policy_stopped" in graphblocks_client.__all__
    assert "remote_tool_result_terminal_event" in graphblocks_client.__all__


def test_client_package_remote_adapter_terminal_events_require_validation(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    admitted, resolved = _remote_admitted_call(
        arguments={"query": "billing"},
        output_schema="schemas/SearchResult@1",
    )
    invalid = graphblocks_client.ToolResult.completed(
        "call-1",
        (graphblocks_client.ContentPart(kind="json", data={"items": [7]}),),
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:03Z",
    )

    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="failed validation"):
        graphblocks_client.remote_tool_result_terminal_event(
            admitted,
            resolved,
            _remote_output_registry(),
            sequence=4,
            result=invalid,
        )

    failed = graphblocks_client.ToolResult.failed(
        "call-other",
        error={"code": "remote.failed", "message": "failed"},
        started_at="2026-06-24T00:00:01Z",
        completed_at="2026-06-24T00:00:03Z",
    )
    with pytest.raises(graphblocks_client.RemoteToolAdapterError, match="terminal event is invalid"):
        graphblocks_client.remote_tool_result_terminal_event(
            admitted,
            resolved,
            _remote_output_registry(),
            sequence=5,
            result=failed,
        )


def test_client_package_runs_local_graph_command_and_emits_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-local-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    client = graphblocks_client.LocalGraphBlocksClient()
    inputs = {"message": {"text": "ok"}}
    command = graphblocks_client.RunGraphCommand(
        graph=graph,
        inputs=inputs,
        run_id="run-client-1",
        release_id="release-1",
        policy_snapshot_id="policy-1",
    )
    graph["spec"]["nodes"]["render"]["config"]["template"] = "Mutated {message.text}"
    inputs["message"]["text"] = "mutated"

    response = client.run_graph(command)

    assert response.status == "succeeded"
    assert response.outputs == {"prompt": "Client ok"}
    assert [event.kind for event in response.events] == ["RunStarted", "RunSucceeded"]
    assert [event.metadata.sequence for event in response.events] == [1, 2]
    assert [event.metadata.cursor for event in response.events] == ["run-client-1:1", "run-client-1:2"]
    assert response.events[0].payload["graph_hash"].startswith("sha256:")
    assert response.events[1].payload == {"status": "succeeded", "outputs": {"prompt": "Client ok"}}
    assert response.event_stream.accept(response.events[0]) == response.events[0]
    assert "RunStarted" in graphblocks_client.STANDARD_APPLICATION_EVENT_KINDS
    assert "LocalGraphBlocksClient" in graphblocks_client.__all__


@pytest.mark.parametrize(
    ("command_kwargs", "message"),
    (
        ({"graph": []}, "run graph command graph must be a JSON object"),
        ({"inputs": []}, "run graph command inputs must be a JSON object"),
        (
            {"graph": {"kind": "Graph", "metadata": {"score": math.nan}}},
            "run graph command graph must contain canonical JSON values",
        ),
        (
            {"inputs": {"message": {"score": math.nan}}},
            "run graph command inputs must contain canonical JSON values",
        ),
        (
            {"inputs": {"message": object()}},
            "run graph command inputs must contain canonical JSON values",
        ),
        (
            {"graph": {"kind": "Graph", "spec": {"nodes": {1: {}}}}},
            "run graph command graph object keys must be non-empty strings",
        ),
        (
            {"inputs": {"message": {"": "blank"}}},
            "run graph command inputs object keys must be non-empty strings",
        ),
        ({"run_id": True}, "run graph command run_id must be a non-empty string"),
        ({"response_id": " "}, "run graph command response_id must be a non-empty string"),
        ({"turn_id": ""}, "run graph command turn_id must be a non-empty string"),
        ({"occurred_at": None}, "run graph command occurred_at must be a string"),
        ({"occurred_at": " "}, "run graph command occurred_at must be a non-empty string"),
    ),
)
def test_client_package_rejects_malformed_run_graph_command(
    monkeypatch,
    command_kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    kwargs: dict[str, object] = {
        "graph": {"kind": "Graph", "metadata": {"name": "remote-run"}},
    }
    kwargs.update(command_kwargs)

    with pytest.raises(ValueError, match=message):
        graphblocks_client.RunGraphCommand(**kwargs)


def test_client_package_response_outputs_are_nested_snapshots(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    outputs = {"answer": {"text": "ok"}}

    response = graphblocks_client.RunGraphResponse(
        run_id="run-1",
        status="succeeded",
        outputs=outputs,
        events=(),
        event_stream=graphblocks_client.ApplicationEventStreamState(),
    )
    outputs["answer"]["text"] = "mutated"

    assert response.outputs == {"answer": {"text": "ok"}}


def test_client_package_posts_run_graph_command_over_http(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    requests: list[object] = []

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "runId": "run-http-1",
                    "status": "succeeded",
                    "outputs": {"answer": "ok"},
                    "events": [
                        {
                            "kind": "RunStarted",
                            "metadata": {
                                "eventId": "event-1",
                                "runId": "run-http-1",
                                "responseId": "response-http-1",
                                "turnId": None,
                                "sequence": 1,
                                "releaseId": "release-1",
                                "policySnapshotId": "policy-1",
                                "occurredAt": "2026-06-24T00:00:00Z",
                            },
                            "payload": {"status": "running"},
                        }
                    ],
                }
            ).encode("utf-8")

    def transport(request: object, *, timeout: float) -> FakeResponse:
        requests.append(request)
        assert timeout == 5.0
        return FakeResponse()

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=5.0,
        transport=transport,
    )

    response = client.run_graph(
        graphblocks_client.RunGraphCommand(
            graph={"kind": "Graph", "metadata": {"name": "remote-run"}},
            inputs={"message": {"text": "hello"}},
            run_id="run-http-1",
            response_id="response-http-1",
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-06-24T00:00:00Z",
        )
    )

    request = requests[0]
    body = json.loads(request.data.decode("utf-8"))
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert request.full_url == "https://graphblocks.example/api/runs"
    assert request.get_method() == "POST"
    assert headers["authorization"] == "Bearer token-1"
    assert headers["content-type"] == "application/json"
    assert body["runId"] == "run-http-1"
    assert body["responseId"] == "response-http-1"
    assert body["inputs"] == {"message": {"text": "hello"}}
    assert response.run_id == "run-http-1"
    assert response.status == "succeeded"
    assert response.outputs == {"answer": "ok"}
    assert response.events[0].kind == "RunStarted"
    assert response.event_stream.accept(response.events[0]) == response.events[0]
    assert "HttpGraphBlocksClient" in graphblocks_client.__all__


@pytest.mark.parametrize(
    ("metadata_override", "message"),
    (
        ({"sequence": True}, "GraphBlocks HTTP event metadata sequence must be an integer"),
        ({"eventId": None}, "GraphBlocks HTTP event metadata event_id must be a non-empty string"),
        ({"occurredAt": None}, "GraphBlocks HTTP event metadata occurred_at must be a string"),
    ),
)
def test_client_package_rejects_malformed_http_event_metadata(
    monkeypatch,
    metadata_override: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    class FakeResponse:
        def read(self) -> bytes:
            metadata = {
                "eventId": "event-1",
                "runId": "run-http-1",
                "responseId": "response-http-1",
                "turnId": None,
                "sequence": 1,
                "releaseId": "release-1",
                "policySnapshotId": "policy-1",
                "occurredAt": "2026-06-24T00:00:00Z",
            }
            metadata.update(metadata_override)
            return json.dumps(
                {
                    "runId": "run-http-1",
                    "status": "succeeded",
                    "outputs": {},
                    "events": [{"kind": "RunStarted", "metadata": metadata, "payload": {"status": "running"}}],
                }
            ).encode("utf-8")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match=message):
        client.run_graph(
            graphblocks_client.RunGraphCommand(
                graph={"kind": "Graph", "metadata": {"name": "remote-run"}},
            )
        )


@pytest.mark.parametrize(
    ("payload_override", "message"),
    (
        ({"events": {}}, "GraphBlocks HTTP response events must be a JSON array"),
        (
            {"events": [{"kind": None, "metadata": {}}]},
            "GraphBlocks HTTP event kind must be a non-empty string",
        ),
        (
            {"events": [{"kind": "RunStarted", "metadata": {}, "payload": []}]},
            "GraphBlocks HTTP event payload must be a JSON object",
        ),
        (
            {"events": [{"kind": "ToolCallStarted", "metadata": {}, "payload": {}, "toolCallId": True}]},
            "GraphBlocks HTTP event tool_call_id must be a non-empty string",
        ),
    ),
)
def test_client_package_rejects_malformed_http_event_payloads(
    monkeypatch,
    payload_override: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    valid_metadata = {
        "eventId": "event-1",
        "runId": "run-http-1",
        "responseId": "response-http-1",
        "turnId": None,
        "sequence": 1,
        "releaseId": "release-1",
        "policySnapshotId": "policy-1",
        "occurredAt": "2026-06-24T00:00:00Z",
    }

    class FakeResponse:
        def read(self) -> bytes:
            payload: dict[str, object] = {
                "runId": "run-http-1",
                "status": "succeeded",
                "outputs": {},
                "events": [
                    {
                        "kind": "RunStarted",
                        "metadata": valid_metadata,
                        "payload": {"status": "running"},
                    }
                ],
            }
            payload.update(payload_override)
            for event_payload in payload.get("events", []) if isinstance(payload.get("events"), list) else []:
                if isinstance(event_payload, dict):
                    if "metadata" not in event_payload or event_payload["metadata"] == {}:
                        event_payload["metadata"] = valid_metadata
            return json.dumps(payload).encode("utf-8")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match=message):
        client.run_graph(
            graphblocks_client.RunGraphCommand(
                graph={"kind": "Graph", "metadata": {"name": "remote-run"}},
            )
        )


@pytest.mark.parametrize(
    ("payload_override", "message"),
    (
        ({"runId": True}, "GraphBlocks HTTP response run_id must be a non-empty string"),
        ({"status": None}, "GraphBlocks HTTP response status must be a non-empty string"),
        ({"outputs": []}, "GraphBlocks HTTP response outputs must be a JSON object"),
    ),
)
def test_client_package_rejects_malformed_http_run_response(
    monkeypatch,
    payload_override: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    class FakeResponse:
        def read(self) -> bytes:
            payload: dict[str, object] = {
                "runId": "run-http-1",
                "status": "succeeded",
                "outputs": {},
                "events": [],
            }
            payload.update(payload_override)
            return json.dumps(payload).encode("utf-8")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match=message):
        client.run_graph(
            graphblocks_client.RunGraphCommand(
                graph={"kind": "Graph", "metadata": {"name": "remote-run"}},
            )
        )


def test_client_package_rejects_malformed_http_stream_response(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "runId": True,
                    "stream": {"status": "accepted"},
                    "events": [],
                }
            ).encode("utf-8")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match="GraphBlocks run stream response run_id must be a non-empty string"):
        client.run_stream("run-http-1")


def test_client_package_rejects_non_standard_http_json_constants(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"ok": NaN}'

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match="GraphBlocks health response must be valid JSON"):
        client.health()


@pytest.mark.parametrize("status_code", (True, "500"))
def test_client_package_rejects_malformed_http_status_code(monkeypatch, status_code: object) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    class FakeResponse:
        status = status_code

        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(ValueError, match="GraphBlocks health response status code must be an integer"):
        client.health()


@pytest.mark.parametrize(
    "method_name",
    (
        "cancel_run",
        "run_status",
        "run_events",
        "run_stream",
        "pause_run",
        "resume_run",
        "expire_run",
        "attach_to_run",
        "detach_from_run",
        "subscribe_events",
        "unsubscribe_events",
        "ack_event",
    ),
)
@pytest.mark.parametrize("run_id", (True, " "))
def test_client_package_rejects_malformed_http_run_id_arguments(
    monkeypatch,
    method_name: str,
    run_id: object,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    def transport(request: object, *, timeout: float) -> object:
        raise AssertionError("transport should not be called for malformed run_id")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=transport,
    )

    with pytest.raises(ValueError, match="GraphBlocks HTTP run_id must be a non-empty string"):
        if method_name == "detach_from_run":
            getattr(client, method_name)(run_id, client_id="client-1")
        elif method_name == "subscribe_events":
            getattr(client, method_name)(run_id, delivery={"kind": "local_callback", "callback_name": "ide"})
        elif method_name == "unsubscribe_events":
            getattr(client, method_name)(run_id, "sub-1")
        elif method_name == "ack_event":
            getattr(client, method_name)(run_id, "sub-1", cursor="run-1:1")
        else:
            getattr(client, method_name)(run_id)


def test_client_package_reads_server_health_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.server import GraphBlocksServerApp, ServerHealth, ServerRequest

    app = GraphBlocksServerApp(
        health=ServerHealth(
            "graphblocks-api",
            checks=(("runtime", "healthy", {"workers": 2}),),
            observed_at="2026-06-24T00:00:00Z",
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 2.5
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path="/health",
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api/",
        timeout=2.5,
        transport=transport,
    )

    health = client.health()

    assert health == {
        "service": "graphblocks-api",
        "status": "healthy",
        "observed_at": "2026-06-24T00:00:00Z",
        "checks": {"runtime": {"status": "healthy", "details": {"workers": 2}}},
    }


def test_client_package_sends_cancel_run_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-http-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-http-1"},
            "metadata": {
                "runId": "run-http-1",
                "sequence": 1,
                "cursor": "run-http-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.cancel_run("run-http-1")

    assert response == {
        "lastCursor": "run-http-1:1",
        "ok": True,
        "reason": None,
        "runId": "run-http-1",
        "status": "cancelled",
    }


def test_client_package_reads_run_status_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-status-http-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-status-http-1"},
            "metadata": {
                "runId": "run-status-http-1",
                "sequence": 1,
                "cursor": "run-status-http-1:1",
                "releaseId": "release-status-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-status-http-1"
        assert headers["authorization"] == "Bearer token-1"
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    status = client.run_status("run-status-http-1")

    assert status["runId"] == "run-status-http-1"
    assert status["state"] == "running"
    assert status["releaseId"] == "release-status-1"
    assert status["lastCursor"] == "run-status-http-1:1"
    assert status["waitingOn"] == []
    assert status["activeOperations"] == []


def test_client_package_lists_runs_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-list-http-2"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-list-http-2"},
            "metadata": {
                "runId": "run-list-http-2",
                "sequence": 1,
                "cursor": "run-list-http-2:1",
                "releaseId": "release-list-2",
                "occurredAt": "2026-07-03T00:00:02Z",
            },
        },
    )
    app._events_by_run_id["run-list-http-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-list-http-1"},
            "metadata": {
                "runId": "run-list-http-1",
                "sequence": 1,
                "cursor": "run-list-http-1:1",
                "releaseId": "release-list-1",
                "occurredAt": "2026-07-03T00:00:01Z",
            },
        },
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs"
        assert request.get_method() == "GET"
        assert headers["authorization"] == "Bearer token-1"
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    payload = client.list_runs()

    assert payload["ok"] is True
    assert [run["runId"] for run in payload["runs"]] == ["run-list-http-1", "run-list-http-2"]
    assert payload["runs"][0]["state"] == "running"
    assert payload["runs"][0]["lastCursor"] == "run-list-http-1:1"


def test_client_package_controls_run_lifecycle_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))
    app._events_by_run_id["run-control-client-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-control-client-1"},
            "metadata": {
                "runId": "run-control-client-1",
                "sequence": 1,
                "cursor": "run-control-client-1:1",
                "releaseId": "release-control-client-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )
    calls: list[str] = []

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert headers["authorization"] == "Bearer token-1"
        if path in {"/runs/run-control-client-1/pause", "/runs/run-control-client-1/expire"}:
            assert headers["content-type"] == "application/json"
        calls.append(path)
        if path == "/runs/run-control-client-1/pause":
            assert json.loads(request.data.decode("utf-8")) == {
                "pauseKind": "budget",
                "reason": "budget extension required",
            }
        if path == "/runs/run-control-client-1/resume":
            assert request.data == b"{}"
        if path == "/runs/run-control-client-1/expire":
            assert json.loads(request.data.decode("utf-8")) == {"reason": "deadline exceeded"}
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at={
                    "/runs/run-control-client-1/pause": "2026-07-03T00:00:01Z",
                    "/runs/run-control-client-1/resume": "2026-07-03T00:00:02Z",
                    "/runs/run-control-client-1/expire": "2026-07-03T00:00:03Z",
                }[path],
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    pause = client.pause_run(
        "run-control-client-1",
        pause_kind="budget",
        reason="budget extension required",
    )
    resume = client.resume_run("run-control-client-1")
    expire = client.expire_run("run-control-client-1", reason="deadline exceeded")

    assert calls == [
        "/runs/run-control-client-1/pause",
        "/runs/run-control-client-1/resume",
        "/runs/run-control-client-1/expire",
    ]
    assert pause == {
        "ok": True,
        "runId": "run-control-client-1",
        "status": "paused_budget",
        "reason": "budget extension required",
        "lastCursor": "run-control-client-1:1",
    }
    assert resume["status"] == "resuming"
    assert expire == {
        "ok": True,
        "runId": "run-control-client-1",
        "status": "expired",
        "reason": "deadline exceeded",
        "lastCursor": "run-control-client-1:1",
    }
    assert [control["status"] for control in app.run_controls("run-control-client-1")] == [
        "paused_budget",
        "resuming",
        "expired",
    ]


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs", "message"),
    (
        (
            "pause_run",
            ("run-1",),
            {"pause_kind": True},
            "GraphBlocks HTTP pause_kind must be a non-empty string",
        ),
        (
            "pause_run",
            ("run-1",),
            {"reason": ""},
            "GraphBlocks HTTP reason must be a non-empty string",
        ),
        (
            "expire_run",
            ("run-1",),
            {"reason": True},
            "GraphBlocks HTTP reason must be a non-empty string",
        ),
    ),
)
def test_client_package_rejects_malformed_run_control_arguments(
    monkeypatch,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    def transport(request: object, *, timeout: float) -> object:
        raise AssertionError("transport should not be called for malformed run control arguments")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=transport,
    )

    with pytest.raises(ValueError, match=message):
        getattr(client, method_name)(*args, **kwargs)


def test_client_package_submits_async_callback_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-client-callback-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-client-callback-1"},
            "metadata": {
                "runId": "run-client-callback-1",
                "sequence": 1,
                "cursor": "run-client-callback-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/callbacks/op-ci-client-1"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert headers["graphblocks-idempotency-key"] == "idem-client-callback-1"
        body = json.loads(request.data.decode("utf-8"))
        assert body == {
            "attemptId": "attempt-1",
            "callbackId": "cb-client-1",
            "nodeId": "waitCI",
            "payload": {"status": "completed", "checks": [{"name": "unit", "passed": True}]},
            "providerOperationId": "provider-ci-1",
            "runId": "run-client-callback-1",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.submit_async_callback(
        operation_id="op-ci-client-1",
        callback_id="cb-client-1",
        idempotency_key="idem-client-callback-1",
        payload={"status": "completed", "checks": [{"name": "unit", "passed": True}]},
        run_id="run-client-callback-1",
        node_id="waitCI",
        attempt_id="attempt-1",
        provider_operation_id="provider-ci-1",
    )

    assert response == {
        "ok": True,
        "operationId": "op-ci-client-1",
        "callbackId": "cb-client-1",
        "idempotencyKey": "idem-client-callback-1",
        "payloadDigest": "sha256:4b7f8e395f509529dbf2eba914e46745cbe72791078d1b9c198d01daab24c9ae",
        "verifiedBy": "callback-relay",
        "policySnapshotId": "local",
        "status": "accepted",
        "runId": "run-client-callback-1",
        "nodeId": "waitCI",
        "attemptId": "attempt-1",
        "providerOperationId": "provider-ci-1",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        (
            {
                "operation_id": " ",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": {"status": "completed"},
            },
            "GraphBlocks HTTP operation_id must be a non-empty string",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": True,
                "idempotency_key": "idem-1",
                "payload": {"status": "completed"},
            },
            "GraphBlocks HTTP callback_id must be a non-empty string",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "",
                "payload": {"status": "completed"},
            },
            "GraphBlocks HTTP idempotency_key must be a non-empty string",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": ["completed"],
            },
            "GraphBlocks HTTP callback payload must be a JSON object",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": {"score": math.nan},
            },
            "GraphBlocks HTTP callback payload must contain canonical JSON values",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": {"status": object()},
            },
            "GraphBlocks HTTP callback payload must contain canonical JSON values",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": {1: "completed"},
            },
            "GraphBlocks HTTP callback payload object keys must be non-empty strings",
        ),
        (
            {
                "operation_id": "op-1",
                "callback_id": "cb-1",
                "idempotency_key": "idem-1",
                "payload": {"result": {"": "completed"}},
            },
            "GraphBlocks HTTP callback payload object keys must be non-empty strings",
        ),
    ),
)
def test_client_package_rejects_malformed_async_callback_arguments(
    monkeypatch,
    kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    def transport(request: object, *, timeout: float) -> object:
        raise AssertionError("transport should not be called for malformed callback arguments")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=transport,
    )

    with pytest.raises(ValueError, match=message):
        client.submit_async_callback(**kwargs)


def test_client_package_reads_run_events_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-events"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client events {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-events-http-1",
                    "responseId": "response-events-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        assert path == "/runs/run-events-http-1/events"
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    events = client.run_events("run-events-http-1")

    assert [event.kind for event in events] == ["RunStarted", "RunSucceeded"]
    assert events[0].metadata.response_id == "response-events-http-1"
    assert events[1].payload["outputs"] == {"prompt": "Client events ok"}


def test_client_package_raises_http_error_for_missing_run_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    try:
        client.run_events("missing-run")
    except graphblocks_client.GraphBlocksHttpError as error:
        assert error.status_code == 404
        assert error.payload == {
            "ok": False,
            "error": "run events not found for run 'missing-run'",
        }
        assert "GraphBlocks HTTP request failed with status 404" in str(error)
    else:  # pragma: no cover - test should fail before this branch.
        raise AssertionError("missing run events response was not raised as an HTTP error")
    assert "GraphBlocksHttpError" in graphblocks_client.__all__


def test_client_package_attaches_to_run_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-attach"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client attach {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-attach-http-1",
                    "responseId": "response-attach-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-attach-http-1/attach"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "capabilities": ["assistant_drafts", "retractions"],
            "lastCursor": "run-attach-http-1:1",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    snapshot = client.attach_to_run(
        "run-attach-http-1",
        last_cursor="run-attach-http-1:1",
        capabilities=("assistant_drafts", "retractions"),
    )

    assert snapshot.run_id == "run-attach-http-1"
    assert snapshot.stream["lastCursor"] == "run-attach-http-1:2"
    assert snapshot.stream["liveCursor"] == "run-attach-http-1:2"
    assert snapshot.stream["replayComplete"] is True
    assert snapshot.stream["capabilities"] == ["assistant_drafts", "retractions"]
    assert [event.kind for event in snapshot.events] == ["RunSucceeded"]
    assert snapshot.events[0].payload["outputs"] == {"prompt": "Client attach ok"}
    assert snapshot.event_stream.accept(snapshot.events[0]) == snapshot.events[0]


def test_client_package_detaches_from_run_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-detach"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client detach {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-detach-http-1",
                    "responseId": "response-detach-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-detach-http-1/detach"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "clientId": "client-1",
            "reason": "tab_closed",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.detach_from_run("run-detach-http-1", client_id="client-1", reason="tab_closed")

    assert response == {
        "ok": True,
        "runId": "run-detach-http-1",
        "clientId": "client-1",
        "reason": "tab_closed",
        "status": "detached",
        "lastCursor": "run-detach-http-1:2",
    }
    assert [event["kind"] for event in app._events_by_run_id["run-detach-http-1"]] == ["RunStarted", "RunSucceeded"]


def test_client_package_subscribes_to_run_events_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-subscribe"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client subscribe {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-subscribe-http-1",
                    "responseId": "response-subscribe-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-subscribe-http-1/subscriptions"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "delivery": {"callback_name": "ide", "kind": "local_callback"},
            "eventFilter": {"types": ["RunSucceeded"]},
            "failurePolicy": "best_effort",
            "replayFromCursor": "run-subscribe-http-1:1",
            "subscriptionId": "sub-client-1",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    snapshot = client.subscribe_events(
        "run-subscribe-http-1",
        subscription_id="sub-client-1",
        event_filter={"types": ["RunSucceeded"]},
        delivery={"kind": "local_callback", "callback_name": "ide"},
        replay_from_cursor="run-subscribe-http-1:1",
        failure_policy="best_effort",
    )

    assert snapshot.run_id == "run-subscribe-http-1"
    assert snapshot.stream["subscriptionId"] == "sub-client-1"
    assert snapshot.stream["status"] == "active"
    assert snapshot.stream["failurePolicy"] == "best_effort"
    assert snapshot.stream["lastCursor"] == "run-subscribe-http-1:2"
    assert snapshot.stream["eventFilter"] == {"types": ["RunSucceeded"], "visibility": ["client"]}
    assert [event.kind for event in snapshot.events] == ["RunSucceeded"]
    assert snapshot.events[0].payload["outputs"] == {"prompt": "Client subscribe ok"}


def test_client_package_unsubscribes_from_run_events_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-unsubscribe"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client unsubscribe {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-unsubscribe-http-1",
                    "responseId": "response-unsubscribe-http-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-unsubscribe-http-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-client-unsubscribe-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-unsubscribe-http-1/subscriptions/sub-client-unsubscribe-1"
        assert headers["authorization"] == "Bearer token-1"
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.unsubscribe_events("run-unsubscribe-http-1", "sub-client-unsubscribe-1")

    assert response == {
        "ok": True,
        "runId": "run-unsubscribe-http-1",
        "subscriptionId": "sub-client-unsubscribe-1",
        "status": "revoked",
    }
    assert app.subscriptions("run-unsubscribe-http-1")[0].status == "revoked"


def test_client_package_acknowledges_subscription_event_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-ack"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client ack {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-ack-http-1",
                    "responseId": "response-ack-http-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-http-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-client-ack-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-ack-http-1/subscriptions/sub-client-ack-1/ack"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {"cursor": "run-ack-http-1:2"}
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.ack_event("run-ack-http-1", "sub-client-ack-1", cursor="run-ack-http-1:2")

    assert response == {
        "ok": True,
        "runId": "run-ack-http-1",
        "subscriptionId": "sub-client-ack-1",
        "eventId": "run-ack-http-1:run-terminal",
        "cursor": "run-ack-http-1:2",
        "status": "acknowledged",
    }
    assert app.event_acks("run-ack-http-1", "sub-client-ack-1") == (
        {
            "eventId": "run-ack-http-1:run-terminal",
            "cursor": "run-ack-http-1:2",
            "acknowledgedAt": "2026-07-03T00:00:00Z",
        },
    )


def test_client_package_registers_callback_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-register-callback"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client callback {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-register-callback-http-1",
                    "responseId": "response-register-callback-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/callbacks/register"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "deadLetterPolicy": "webhook-standard",
            "delivery": {
                "kind": "webhook",
                "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                "url": "https://relay.example/events",
            },
            "eventFilter": {"types": ["RunSucceeded"]},
            "failurePolicy": "retry_then_dead_letter",
            "replayFromCursor": "run-register-callback-http-1:1",
            "scope": "run",
            "scopeId": "run-register-callback-http-1",
            "subscriptionId": "callback-sub-client-1",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    snapshot = client.register_callback(
        subscription_id="callback-sub-client-1",
        scope="run",
        scope_id="run-register-callback-http-1",
        event_filter={"types": ["RunSucceeded"]},
        delivery={
            "kind": "webhook",
            "url": "https://relay.example/events",
            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
        },
        replay_from_cursor="run-register-callback-http-1:1",
        failure_policy="retry_then_dead_letter",
        dead_letter_policy="webhook-standard",
    )

    assert snapshot.run_id == "run-register-callback-http-1"
    assert snapshot.stream["subscriptionId"] == "callback-sub-client-1"
    assert snapshot.stream["scope"] == "run"
    assert snapshot.stream["scopeId"] == "run-register-callback-http-1"
    assert snapshot.stream["lastCursor"] == "run-register-callback-http-1:2"
    assert [event.kind for event in snapshot.events] == ["RunSucceeded"]
    assert snapshot.events[0].payload["outputs"] == {"prompt": "Client callback ok"}


def test_client_package_revokes_callback_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-revoke-client-1",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                    "deadLetterPolicy": "webhook-standard",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/callbacks/callback-sub-revoke-client-1"
        assert headers["authorization"] == "Bearer token-1"
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.revoke_callback("callback-sub-revoke-client-1")

    assert response == {
        "ok": True,
        "subscriptionId": "callback-sub-revoke-client-1",
        "status": "revoked",
    }
    assert app.callback_registrations()[0].status == "revoked"


def test_client_package_redrives_callback_delivery_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/callbacks/deliveries/del-client-1/redrive"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "operator": "operator-1",
            "reason": "receiver recovered",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:00:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.redrive_callback_delivery(
        "del-client-1",
        operator="operator-1",
        reason="receiver recovered",
    )

    assert response == {
        "ok": True,
        "deliveryId": "del-client-1",
        "operator": "operator-1",
        "reason": "receiver recovered",
        "status": "redrive_requested",
    }
    assert app.callback_delivery_redrives("del-client-1") == (
        {
            "deliveryId": "del-client-1",
            "operator": "operator-1",
            "reason": "receiver recovered",
            "requestedAt": "2026-07-03T00:00:00Z",
            "status": "redrive_requested",
        },
    )


def test_client_package_moves_callback_delivery_to_dead_letter_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/callbacks/deliveries/del-client-2/dead-letter"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data.decode("utf-8")) == {
            "operator": "operator-1",
            "reason": "max attempts exhausted",
        }
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
                requested_at="2026-07-03T00:01:00Z",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    response = client.move_callback_to_dead_letter(
        "del-client-2",
        operator="operator-1",
        reason="max attempts exhausted",
    )
    duplicate = client.move_callback_to_dead_letter(
        "del-client-2",
        operator="operator-1",
        reason="max attempts exhausted",
    )

    assert response == {
        "ok": True,
        "deliveryId": "del-client-2",
        "operator": "operator-1",
        "reason": "max attempts exhausted",
        "status": "dead_letter_requested",
    }
    assert duplicate == {
        "ok": True,
        "deliveryId": "del-client-2",
        "operator": "operator-1",
        "reason": "max attempts exhausted",
        "status": "dead_letter_requested",
        "requestedAt": "2026-07-03T00:01:00Z",
        "duplicate": True,
    }
    assert app.callback_delivery_dead_letter_moves("del-client-2") == (
        {
            "deliveryId": "del-client-2",
            "operator": "operator-1",
            "reason": "max attempts exhausted",
            "requestedAt": "2026-07-03T00:01:00Z",
            "status": "dead_letter_requested",
        },
    )


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs", "message"),
    (
        (
            "subscribe_events",
            ("run-1",),
            {
                "event_filter": {1: ["RunSucceeded"]},
                "delivery": {"kind": "local_callback", "callback_name": "ide"},
            },
            "GraphBlocks HTTP event_filter object keys must be non-empty strings",
        ),
        (
            "subscribe_events",
            ("run-1",),
            {
                "event_filter": {"types": ["RunSucceeded"]},
                "delivery": {"kind": "webhook", "url": "https://relay.example/events", "headers": {"": "x"}},
            },
            "GraphBlocks HTTP delivery object keys must be non-empty strings",
        ),
        (
            "register_callback",
            (),
            {
                "scope": "run",
                "scope_id": "run-1",
                "event_filter": {"types": ["RunSucceeded"], "limits": {"max": math.nan}},
                "delivery": {"kind": "local_callback", "callback_name": "ide"},
            },
            "GraphBlocks HTTP event_filter must contain canonical JSON values",
        ),
        (
            "register_callback",
            (),
            {
                "scope": "run",
                "scope_id": "run-1",
                "event_filter": {"types": ["RunSucceeded"]},
                "delivery": {"kind": "webhook", "url": "https://relay.example/events", "headers": {1: "x"}},
            },
            "GraphBlocks HTTP delivery object keys must be non-empty strings",
        ),
    ),
)
def test_client_package_rejects_malformed_callback_subscription_config(
    monkeypatch,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    def transport(request: object, *, timeout: float) -> object:
        raise AssertionError("transport should not be called for malformed callback subscription config")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=transport,
    )

    with pytest.raises(ValueError, match=message):
        getattr(client, method_name)(*args, **kwargs)


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs", "message"),
    (
        (
            "redrive_callback_delivery",
            (" ",),
            {"operator": "operator-1", "reason": "receiver recovered"},
            "GraphBlocks HTTP delivery_id must be a non-empty string",
        ),
        (
            "move_callback_to_dead_letter",
            ("del-1",),
            {"operator": True, "reason": "max attempts exhausted"},
            "GraphBlocks HTTP operator must be a non-empty string",
        ),
        (
            "redrive_callback_delivery",
            ("del-1",),
            {"operator": "operator-1", "reason": ""},
            "GraphBlocks HTTP reason must be a non-empty string",
        ),
    ),
)
def test_client_package_rejects_malformed_callback_delivery_control_arguments(
    monkeypatch,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")

    def transport(request: object, *, timeout: float) -> object:
        raise AssertionError("transport should not be called for malformed delivery control arguments")

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        transport=transport,
    )

    with pytest.raises(ValueError, match=message):
        getattr(client, method_name)(*args, **kwargs)


def test_client_package_opens_run_stream_over_http_transport(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    from graphblocks.policy import PrincipalRef
    from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "client-stream"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Client stream {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": "run-stream-http-1",
                    "responseId": "response-stream-http-1",
                }
            ).encode("utf-8"),
        )
    )

    def transport(request: object, *, timeout: float) -> object:
        assert timeout == 4.0
        path = urlparse(request.full_url).path.removeprefix("/api")
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert path == "/runs/run-stream-http-1/stream"
        assert headers["authorization"] == "Bearer token-1"
        assert headers["upgrade"] == "websocket"
        assert "Upgrade" in headers["connection"]
        return app.handle(
            ServerRequest(
                method=request.get_method(),
                path=path,
                headers=dict(request.headers),
                query={},
                cookies={},
                body=request.data or b"",
            )
        )

    client = graphblocks_client.HttpGraphBlocksClient(
        "https://graphblocks.example/api",
        bearer_token="token-1",
        timeout=4.0,
        transport=transport,
    )

    snapshot = client.run_stream("run-stream-http-1")

    assert snapshot.run_id == "run-stream-http-1"
    assert snapshot.stream == {
        "transport": "websocket",
        "status": "accepted",
        "cursor": "run-stream-http-1:2",
        "eventCount": 2,
    }
    assert [event.kind for event in snapshot.events] == ["RunStarted", "RunSucceeded"]
    assert snapshot.events[0].metadata.response_id == "response-stream-http-1"
    assert snapshot.events[1].payload["outputs"] == {"prompt": "Client stream ok"}
    assert "RunStreamSnapshot" in graphblocks_client.__all__
