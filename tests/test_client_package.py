from __future__ import annotations

from dataclasses import replace
import importlib
import json
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
        idempotency="not_applicable",
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
    return AdmittedToolCall(call=call, idempotency_key="idem-1"), resolved


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
        payload={"response_id": "response-1", "last_client_delivered_sequence": 1},
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
    stream = graphblocks_client.ToolResultStreamState()
    stopped_event = graphblocks_client.ToolResultEvent.policy_stopped(
        admitted.call.tool_call_id,
        4,
        stopped,
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
    assert "ToolResultEvent" in graphblocks_client.__all__
    assert "ToolResultStreamState" in graphblocks_client.__all__
    assert "ToolResultStreamError" in graphblocks_client.__all__
    assert "remote_tool_result_policy_stopped" in graphblocks_client.__all__


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


@pytest.mark.parametrize("method_name", ("cancel_run", "run_events", "run_stream"))
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
        "ok": True,
        "runId": "run-http-1",
        "status": "cancel_requested",
    }


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
