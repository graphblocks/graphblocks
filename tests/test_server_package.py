from __future__ import annotations

import importlib
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_server_package_reexports_framework_neutral_contracts(monkeypatch) -> None:
    graphblocks_server = importlib.import_module("graphblocks.server")

    manifest = graphblocks_server.default_server_route_manifest()
    capabilities = graphblocks_server.ApplicationProtocolCapabilities("graphblocks.app.v1").with_commands(
        ["invoke_graph"]
    )
    command = graphblocks_server.ApplicationCommand.new(
        "InvokeGraph",
        graphblocks_server.ApplicationCommandMetadata(
            command_id="cmd-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=1,
            issued_at_unix_ms=10,
            idempotency_key="idem-1",
        ),
        payload={"graph_id": "support-agent-turn"},
    )
    event = graphblocks_server.ApplicationProtocolEvent.new(
        "RunStarted",
        graphblocks_server.ApplicationProtocolEventMetadata(
            event_id="evt-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=1,
            occurred_at_unix_ms=11,
            cursor="1",
        ),
        payload={"run_id": "run-1"},
    )
    log = graphblocks_server.ApplicationProtocolLog()

    assert manifest.lookup("GET", "/health").operation == "health"
    assert capabilities.protocol_version == "graphblocks.app.v1"
    assert command.payload["graph_id"] == "support-agent-turn"
    assert event.payload["run_id"] == "run-1"
    assert log.append(event) is True
    assert log.replay_after(limit=1) == (event,)
    assert "InvokeGraph" in graphblocks_server.APPLICATION_COMMAND_KINDS
    assert "RunStarted" in graphblocks_server.APPLICATION_PROTOCOL_EVENT_KINDS
    assert graphblocks_server.GraphBlocksServerApp().handle(
        graphblocks_server.ServerRequest(
            method="GET",
            path="/health",
            headers={},
            query={},
            cookies={},
        )
    ).status_code == 200
    assert graphblocks_server.ServerAsyncCallbackSubmission(
        operation_id="op-1",
        callback_id="cb-1",
        idempotency_key="idem-1",
        payload={"status": "completed"},
    ).response_payload()["status"] == "accepted"
    assert graphblocks_server.ServerEventSubscription(
        subscription_id="sub-1",
        run_id="run-1",
        event_filter={"types": ["RunSucceeded"]},
        delivery={"kind": "local_callback", "callback_name": "ide"},
    ).status == "active"
    assert graphblocks_server.ServerCallbackRegistration(
        subscription_id="callback-sub-1",
        scope="run",
        scope_id="run-1",
        event_filter={"types": ["RunSucceeded"]},
        delivery={
            "kind": "webhook",
            "url": "https://relay.example/events",
            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://callbacks/relay"},
        },
    ).status == "active"
    assert "ApplicationCommand" in graphblocks_server.__all__
    assert "ApplicationProtocolEvent" in graphblocks_server.__all__
    assert "ApplicationProtocolLog" in graphblocks_server.__all__
    assert "ServerAsyncCallbackSubmission" in graphblocks_server.__all__
    assert "ServerCallbackRegistration" in graphblocks_server.__all__
    assert "ServerEventSubscription" in graphblocks_server.__all__
    assert "ServerResponse" in graphblocks_server.__all__


def test_server_package_rejects_malformed_run_metadata(monkeypatch) -> None:
    graphblocks_server = importlib.import_module("graphblocks.server")

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-run-validation"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Server validation {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    app = graphblocks_server.GraphBlocksServerApp()

    response = app.handle(
        graphblocks_server.ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "ok"}},
                    "runId": True,
                }
            ).encode("utf-8"),
        )
    )
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 400
    assert payload["error"] == "run request runId must be a string"
