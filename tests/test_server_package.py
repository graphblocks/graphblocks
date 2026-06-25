from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_server_package_reexports_framework_neutral_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-server" / "src"))
    graphblocks_server = importlib.import_module("graphblocks_server")

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

    assert manifest.lookup("GET", "/health").operation == "health"
    assert capabilities.protocol_version == "graphblocks.app.v1"
    assert command.payload["graph_id"] == "support-agent-turn"
    assert event.payload["run_id"] == "run-1"
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
    assert "ApplicationCommand" in graphblocks_server.__all__
    assert "ApplicationProtocolEvent" in graphblocks_server.__all__
    assert "ServerResponse" in graphblocks_server.__all__
