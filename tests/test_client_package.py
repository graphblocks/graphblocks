from __future__ import annotations

import importlib
import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).parents[1]


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

    assert command.payload == {"graph": "support-agent-turn"}
    assert event.kind == "AssistantDraftDelta"
    assert "RequestSnapshot" in graphblocks_client.APPLICATION_COMMAND_KINDS
    assert "AssistantDraftDelta" in graphblocks_client.APPLICATION_PROTOCOL_EVENT_KINDS
    assert "ApplicationCommand" in graphblocks_client.__all__
    assert "ApplicationProtocolEvent" in graphblocks_client.__all__


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
