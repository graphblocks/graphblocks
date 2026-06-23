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

    assert manifest.lookup("GET", "/health").operation == "health"
    assert capabilities.protocol_version == "graphblocks.app.v1"
