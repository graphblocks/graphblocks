from __future__ import annotations

import json

import graphblocks
import pytest

from graphblocks.policy import PrincipalRef
from graphblocks.server import (
    ApplicationProtocolCapabilities,
    GraphBlocksServerApp,
    ServerAuthRequest,
    ServerHealth,
    ServerRequest,
    ServerProtocolVersionMismatchError,
    ServerRouteManifest,
    StaticBearerAuthHook,
    default_server_route_manifest,
)


def test_server_route_manifest_groups_routes_and_hashes_stably() -> None:
    left = default_server_route_manifest().with_endpoint(
        "GET",
        "/artifacts/{artifact_id}",
        "http",
        "open_artifact",
        auth_required=True,
    )
    right = ServerRouteManifest(tuple(reversed(left.endpoints)))

    assert [endpoint.operation for endpoint in left.by_transport("sse")] == ["application_events"]
    assert left.lookup("GET", "/health").operation == "health"
    assert left.lookup("GET", "/health").auth_required is False
    assert left.content_digest() == right.content_digest()


def test_static_bearer_auth_hook_authorizes_configured_principal() -> None:
    hook = StaticBearerAuthHook({"token-1": PrincipalRef("user-1", roles=("operator",))})
    route = default_server_route_manifest().lookup("POST", "/runs")

    allowed = hook.authorize(
        ServerAuthRequest(
            route=route,
            headers={"authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:00:00Z",
        )
    )
    denied = hook.authorize(
        ServerAuthRequest(
            route=route,
            headers={"authorization": "Bearer missing"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:00:01Z",
        )
    )
    public = hook.authorize(
        ServerAuthRequest(
            route=default_server_route_manifest().lookup("GET", "/health"),
            headers={},
            query={},
            cookies={},
            requested_at="2026-06-24T00:00:02Z",
        )
    )

    assert allowed.allowed
    assert allowed.principal == PrincipalRef("user-1", roles=("operator",))
    assert not denied.allowed
    assert denied.reason_codes == ("auth.invalid_bearer_token",)
    assert public.allowed
    assert public.principal is None


def test_server_health_aggregates_component_status() -> None:
    health = ServerHealth(
        service="graphblocks-api",
        checks=(
            ("runtime", "healthy", {"workers": 2}),
            ("event_stream", "degraded", {"lag_ms": 250}),
        ),
        observed_at="2026-06-24T00:00:00Z",
    )

    payload = health.to_payload()

    assert health.overall_status() == "degraded"
    assert payload["status"] == "degraded"
    assert payload["checks"]["runtime"]["status"] == "healthy"
    assert payload["checks"]["event_stream"]["details"] == {"lag_ms": 250}


def test_application_protocol_capabilities_negotiate_intersection() -> None:
    server = (
        ApplicationProtocolCapabilities("graphblocks.app.v1")
        .with_commands(["invoke_graph", "cancel_run"])
        .with_events(["RunStarted", "RunCompleted"])
    )
    client = (
        ApplicationProtocolCapabilities("graphblocks.app.v1")
        .with_commands(["cancel_run", "open_artifact"])
        .with_events(["RunCompleted", "ArtifactReady"])
    )

    negotiated = server.negotiate(client)

    assert negotiated.commands == ("cancel_run",)
    assert negotiated.events == ("RunCompleted",)

    with pytest.raises(ServerProtocolVersionMismatchError) as error:
        server.negotiate(ApplicationProtocolCapabilities("graphblocks.app.v2"))

    assert error.value.left == "graphblocks.app.v1"
    assert error.value.right == "graphblocks.app.v2"


def test_server_app_handles_health_auth_and_run_requests() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}),
        health=ServerHealth(
            "graphblocks-api",
            checks=(("runtime", "healthy", {"workers": 1}),),
            observed_at="2026-06-24T00:00:00Z",
        ),
    )
    health = app.handle(
        ServerRequest(
            method="GET",
            path="/health",
            headers={},
            query={},
            cookies={},
            body=b"",
            requested_at="2026-06-24T00:00:00Z",
        )
    )

    assert health.status_code == 200
    assert json.loads(health.body.decode("utf-8"))["status"] == "healthy"

    denied = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=b"{}",
            requested_at="2026-06-24T00:00:01Z",
        )
    )

    assert denied.status_code == 401
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "reasonCodes": ["auth.missing_bearer_token"],
    }

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Server {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    run = app.handle(
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
                    "runId": "run-server-1",
                    "responseId": "response-server-1",
                    "releaseId": "release-1",
                    "policySnapshotId": "policy-1",
                    "occurredAt": "2026-06-24T00:00:02Z",
                }
            ).encode("utf-8"),
            requested_at="2026-06-24T00:00:02Z",
        )
    )

    payload = json.loads(run.body.decode("utf-8"))
    assert run.status_code == 200
    assert payload["runId"] == "run-server-1"
    assert payload["status"] == "succeeded"
    assert payload["outputs"] == {"prompt": "Server ok"}
    assert [event["kind"] for event in payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert payload["events"][0]["metadata"]["responseId"] == "response-server-1"
    assert graphblocks.GraphBlocksServerApp is GraphBlocksServerApp
    assert "ServerResponse" in graphblocks.__all__
