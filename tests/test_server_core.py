from __future__ import annotations

import json

import graphblocks
import pytest

from graphblocks.policy import PrincipalRef
from graphblocks.server import (
    ApplicationProtocolCapabilities,
    GraphBlocksServerApp,
    ServerAuthRequest,
    ServerEndpoint,
    ServerHealth,
    ServerRequest,
    ServerResponse,
    ServerProtocolVersionMismatchError,
    ServerRouteMatch,
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


def test_server_route_manifest_validates_endpoint_contracts() -> None:
    with pytest.raises(ValueError, match="server endpoint method must not be empty"):
        ServerEndpoint(" ", "/runs", "http", "invoke_graph")
    with pytest.raises(ValueError, match="server endpoint path must start"):
        ServerEndpoint("GET", "runs", "http", "invoke_graph")
    with pytest.raises(ValueError, match="server transport must be one of"):
        ServerEndpoint("GET", "/runs", "grpc", "invoke_graph")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server endpoint operation must not be empty"):
        ServerEndpoint("GET", "/runs", "http", " ")
    with pytest.raises(ValueError, match="server endpoint auth_required must be a boolean"):
        ServerEndpoint("GET", "/runs", "http", "invoke_graph", auth_required="yes")  # type: ignore[arg-type]

    endpoint = ServerEndpoint("GET", "/runs", "http", "invoke_graph")

    with pytest.raises(ValueError, match="endpoints must be ServerEndpoint"):
        ServerRouteManifest((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate server endpoint"):
        ServerRouteManifest((endpoint, endpoint))
    with pytest.raises(ValueError, match="server transport must be one of"):
        default_server_route_manifest().by_transport("grpc")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server route lookup path must start"):
        default_server_route_manifest().match("GET", "health")


def test_server_route_manifest_matches_templated_run_paths() -> None:
    match = default_server_route_manifest().match("POST", "/runs/run-123/cancel")

    assert match.endpoint.operation == "cancel_run"
    assert match.path_params == {"run_id": "run-123"}
    with pytest.raises(TypeError):
        match.path_params["run_id"] = "mutated"
    assert default_server_route_manifest().lookup("POST", "/runs/run-123/cancel").operation == "cancel_run"

    endpoint = default_server_route_manifest().lookup("POST", "/runs/{run_id}/cancel")
    with pytest.raises(ValueError, match="server route path_params must be a mapping"):
        ServerRouteMatch(endpoint, path_params=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={" ": "run-123"})
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={"run_id": object()})  # type: ignore[dict-item]


def test_static_bearer_auth_hook_authorizes_configured_principal() -> None:
    principals_by_token = {"token-1": PrincipalRef("user-1", roles=("operator",))}
    hook = StaticBearerAuthHook(principals_by_token)
    principals_by_token["token-1"] = PrincipalRef("mutated")
    route = default_server_route_manifest().lookup("POST", "/runs")

    with pytest.raises(TypeError):
        hook.principals_by_token["token-2"] = PrincipalRef("user-2")

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


def test_static_bearer_auth_hook_validates_principal_map() -> None:
    with pytest.raises(ValueError, match="principals_by_token must be a mapping"):
        StaticBearerAuthHook([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="static bearer auth token must not be empty"):
        StaticBearerAuthHook({" ": PrincipalRef("user-1")})
    with pytest.raises(ValueError, match="principals must be PrincipalRef"):
        StaticBearerAuthHook({"token-1": object()})  # type: ignore[arg-type]


def test_server_health_aggregates_component_status() -> None:
    runtime_details = {"workers": 2}
    health = ServerHealth(
        service="graphblocks-api",
        checks=(
            ("runtime", "healthy", runtime_details),
            ("event_stream", "degraded", {"lag_ms": 250}),
        ),
        observed_at="2026-06-24T00:00:00Z",
    )
    runtime_details["workers"] = 99

    payload = health.to_payload()
    payload["checks"]["runtime"]["details"]["workers"] = 42

    assert health.overall_status() == "degraded"
    assert health.to_payload()["status"] == "degraded"
    assert health.to_payload()["checks"]["runtime"]["status"] == "healthy"
    assert health.to_payload()["checks"]["runtime"]["details"] == {"workers": 2}
    assert health.to_payload()["checks"]["event_stream"]["details"] == {"lag_ms": 250}


def test_server_request_and_response_maps_are_read_only_snapshots() -> None:
    headers = {"Authorization": "Bearer token-1"}
    query = {"cursor": "1"}
    cookies = {"session": "s1"}
    request = ServerRequest(
        method="GET",
        path="/health",
        headers=headers,
        query=query,
        cookies=cookies,
        body=b"",
        requested_at="2026-06-24T00:00:00Z",
    )
    headers["Authorization"] = "Bearer mutated"
    query["cursor"] = "2"
    cookies["session"] = "s2"

    assert request.headers == {"authorization": "Bearer token-1"}
    assert request.query == {"cursor": "1"}
    assert request.cookies == {"session": "s1"}
    with pytest.raises(TypeError):
        request.headers["authorization"] = "Bearer changed"
    with pytest.raises(TypeError):
        request.query["cursor"] = "changed"
    with pytest.raises(TypeError):
        request.cookies["session"] = "changed"

    response = ServerResponse.json(200, {"ok": True})

    assert response.headers == {"content-type": "application/json"}
    with pytest.raises(TypeError):
        response.headers["content-type"] = "text/plain"


def test_server_request_auth_and_response_validate_contracts() -> None:
    with pytest.raises(ValueError, match="server request method must not be empty"):
        ServerRequest(method=" ", path="/health", headers={}, query={}, cookies={})
    with pytest.raises(ValueError, match="server request path must start"):
        ServerRequest(method="GET", path="health", headers={}, query={}, cookies={})
    with pytest.raises(ValueError, match="server request headers key must not be empty"):
        ServerRequest(method="GET", path="/health", headers={" ": "value"}, query={}, cookies={})
    with pytest.raises(ValueError, match="server request headers values must be strings"):
        ServerRequest(method="GET", path="/health", headers={"x": object()}, query={}, cookies={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server request query values must be strings"):
        ServerRequest(method="GET", path="/health", headers={}, query={"cursor": object()}, cookies={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server request cookies values must be strings"):
        ServerRequest(method="GET", path="/health", headers={}, query={}, cookies={"session": object()})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server request body must be bytes"):
        ServerRequest(method="GET", path="/health", headers={}, query={}, cookies={}, body=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server request requested_at must not be empty"):
        ServerRequest(method="GET", path="/health", headers={}, query={}, cookies={}, requested_at=" ")

    route = default_server_route_manifest().lookup("GET", "/health")

    with pytest.raises(ValueError, match="server auth request route must be a ServerEndpoint"):
        ServerAuthRequest(route=object(), headers={}, query={}, cookies={}, requested_at="")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server auth request headers values must be strings"):
        ServerAuthRequest(
            route=route,
            headers={"authorization": object()},  # type: ignore[arg-type]
            query={},
            cookies={},
            requested_at="",
        )
    with pytest.raises(ValueError, match="server auth request requested_at must not be empty"):
        ServerAuthRequest(route=route, headers={}, query={}, cookies={}, requested_at=" ")

    with pytest.raises(ValueError, match="status_code must be an integer"):
        ServerResponse(status_code=True, headers={}, body=b"")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="status_code must be a valid HTTP status"):
        ServerResponse(status_code=99, headers={}, body=b"")
    with pytest.raises(ValueError, match="server response headers values must be strings"):
        ServerResponse(status_code=200, headers={"x": object()}, body=b"")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server response body must be bytes"):
        ServerResponse(status_code=200, headers={}, body=object())  # type: ignore[arg-type]


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

    with pytest.raises(ValueError, match="application protocol capabilities protocol_version must not be empty"):
        ApplicationProtocolCapabilities(" ")
    with pytest.raises(ValueError, match="application protocol capabilities commands must not be empty"):
        ApplicationProtocolCapabilities("graphblocks.app.v1", commands=(" ",))
    with pytest.raises(ValueError, match="application protocol capabilities events must be a sequence"):
        ApplicationProtocolCapabilities("graphblocks.app.v1", events="RunStarted")  # type: ignore[arg-type]


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


def test_server_app_handles_authenticated_cancel_request() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-server-1/cancel",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:00:03Z",
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-server-1",
        "status": "cancel_requested",
    }


def test_server_app_serves_stored_run_events_after_invocation() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-events"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Events {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

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
                    "runId": "run-events-1",
                    "responseId": "response-events-1",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["runId"] == "run-events-1"
    assert [event["kind"] for event in payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert payload["events"][0]["metadata"]["responseId"] == "response-events-1"


def test_server_app_reports_missing_run_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/missing-run/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run events not found for run 'missing-run'",
    }


def test_server_app_requires_websocket_upgrade_for_application_stream() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-stream-1/stream",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 426
    assert response.headers["upgrade"] == "websocket"
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "application stream requires websocket upgrade",
        "runId": "run-stream-1",
        "requiredTransport": "websocket",
    }


def test_server_app_serves_application_stream_snapshot_for_existing_run() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-stream"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Stream {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
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
                    "runId": "run-stream-1",
                    "responseId": "response-stream-1",
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-stream-1/stream",
            headers={
                "Authorization": "Bearer token-1",
                "Connection": "Upgrade",
                "Upgrade": "websocket",
            },
            query={},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["runId"] == "run-stream-1"
    assert payload["stream"] == {
        "transport": "websocket",
        "status": "accepted",
        "cursor": "run-stream-1:2",
        "eventCount": 2,
    }
    assert [event["kind"] for event in payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert all(event["metadata"]["occurredAt"] for event in payload["events"])
    assert payload["events"][1]["payload"]["outputs"] == {"prompt": "Stream ok"}
