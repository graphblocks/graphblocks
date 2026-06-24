from __future__ import annotations

import importlib
import json
from pathlib import Path


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

    response = client.run_graph(
        graphblocks_client.RunGraphCommand(
            graph=graph,
            inputs={"message": {"text": "ok"}},
            run_id="run-client-1",
            release_id="release-1",
            policy_snapshot_id="policy-1",
        )
    )

    assert response.status == "succeeded"
    assert response.outputs == {"prompt": "Client ok"}
    assert [event.kind for event in response.events] == ["RunStarted", "RunSucceeded"]
    assert [event.metadata.sequence for event in response.events] == [1, 2]
    assert response.events[0].payload["graph_hash"].startswith("sha256:")
    assert response.events[1].payload == {"status": "succeeded", "outputs": {"prompt": "Client ok"}}
    assert response.event_stream.accept(response.events[0]) == response.events[0]
    assert "RunStarted" in graphblocks_client.STANDARD_APPLICATION_EVENT_KINDS
    assert "LocalGraphBlocksClient" in graphblocks_client.__all__


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
