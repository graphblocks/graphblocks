from __future__ import annotations

from itertools import permutations
import json
import math

import graphblocks
import pytest

from graphblocks.policy import PrincipalRef
from graphblocks.server import (
    ApplicationProtocolCapabilities,
    GraphBlocksServerApp,
    ServerAsyncCallbackSubmission,
    ServerAuthRequest,
    ServerCallbackRegistration,
    ServerEndpoint,
    ServerEventSubscription,
    ServerHealth,
    ServerRequest,
    ServerResponse,
    ServerProtocolVersionMismatchError,
    ServerRouteMatch,
    ServerRouteManifest,
    StaticBearerAuthHook,
    default_server_route_manifest,
)


def _callback_rejection_metadata(
    payload: dict[str, object],
    *,
    verified_by: str = "callback-relay",
    policy_snapshot_id: str = "local",
) -> dict[str, object]:
    return {
        "payloadDigest": graphblocks.canonical_hash(payload),
        "verifiedBy": verified_by,
        "policySnapshotId": policy_snapshot_id,
    }


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
    assert left.lookup("GET", "/runs").operation == "list_runs"
    assert left.lookup("POST", "/runs").operation == "invoke_graph"
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
    assert default_server_route_manifest().match("POST", "/runs/run-123/pause").endpoint.operation == "pause_run"
    assert default_server_route_manifest().match("POST", "/runs/run-123/resume").endpoint.operation == "resume_run"
    assert default_server_route_manifest().match("POST", "/runs/run-123/expire").endpoint.operation == "expire_run"
    assert default_server_route_manifest().match("GET", "/runs/run-123/ws").endpoint.operation == "application_stream"

    endpoint = default_server_route_manifest().lookup("POST", "/runs/{run_id}/cancel")
    with pytest.raises(ValueError, match="server route path_params must be a mapping"):
        ServerRouteMatch(endpoint, path_params=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={" ": "run-123"})
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={"run_id": object()})  # type: ignore[dict-item]


def test_server_route_manifest_decodes_encoded_path_parameters() -> None:
    run_match = default_server_route_manifest().match(
        "DELETE",
        "/runs/run%2Fwith%3Fquery%23fragment/subscriptions/sub%2Fwith%3Fquery%23fragment",
    )
    callback_match = default_server_route_manifest().match(
        "POST",
        "/callbacks/op%2Fwith%3Fquery%23fragment",
    )
    delivery_match = default_server_route_manifest().match(
        "POST",
        "/callbacks/deliveries/del%2Fwith%3Fquery%23fragment/redrive",
    )

    assert run_match.path_params == {
        "run_id": "run/with?query#fragment",
        "subscription_id": "sub/with?query#fragment",
    }
    assert callback_match.path_params == {"operation_id": "op/with?query#fragment"}
    assert delivery_match.path_params == {"delivery_id": "del/with?query#fragment"}


def test_server_route_manifest_matches_run_status_path() -> None:
    route_match = default_server_route_manifest().match("GET", "/runs/run-123")

    assert route_match.endpoint.operation == "get_run_status"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123"}


def test_server_route_manifest_matches_attach_to_run_path() -> None:
    route_match = default_server_route_manifest().match("POST", "/runs/run-123/attach")

    assert route_match.endpoint.operation == "attach_to_run"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123"}


def test_server_route_manifest_matches_detach_from_run_path() -> None:
    route_match = default_server_route_manifest().match("POST", "/runs/run-123/detach")

    assert route_match.endpoint.operation == "detach_from_run"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123"}


def test_server_route_manifest_matches_subscribe_events_path() -> None:
    route_match = default_server_route_manifest().match("POST", "/runs/run-123/subscriptions")

    assert route_match.endpoint.operation == "subscribe_events"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123"}


def test_server_route_manifest_matches_unsubscribe_events_path() -> None:
    route_match = default_server_route_manifest().match("DELETE", "/runs/run-123/subscriptions/sub-123")

    assert route_match.endpoint.operation == "unsubscribe_events"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123", "subscription_id": "sub-123"}


def test_server_route_manifest_matches_ack_event_path() -> None:
    route_match = default_server_route_manifest().match("POST", "/runs/run-123/subscriptions/sub-123/ack")

    assert route_match.endpoint.operation == "ack_event"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"run_id": "run-123", "subscription_id": "sub-123"}


def test_server_route_manifest_matches_callback_registration_paths() -> None:
    register_match = default_server_route_manifest().match("POST", "/callbacks/register")
    revoke_match = default_server_route_manifest().match("DELETE", "/callbacks/callback-sub-1")

    assert register_match.endpoint.operation == "register_callback"
    assert register_match.endpoint.auth_required is True
    assert revoke_match.endpoint.operation == "revoke_callback"
    assert revoke_match.path_params == {"subscription_id": "callback-sub-1"}


def test_server_route_manifest_matches_async_callback_ingress_path() -> None:
    route_match = default_server_route_manifest().match("POST", "/callbacks/op-ci-1")

    assert route_match.endpoint.operation == "submit_async_callback"
    assert route_match.endpoint.auth_required is True
    assert route_match.path_params == {"operation_id": "op-ci-1"}


def test_server_route_manifest_matches_callback_delivery_control_paths() -> None:
    redrive_match = default_server_route_manifest().match("POST", "/callbacks/deliveries/del-1/redrive")
    dead_letter_match = default_server_route_manifest().match("POST", "/callbacks/deliveries/del-1/dead-letter")

    assert redrive_match.endpoint.operation == "redrive_callback_delivery"
    assert redrive_match.endpoint.auth_required is True
    assert redrive_match.path_params == {"delivery_id": "del-1"}
    assert dead_letter_match.endpoint.operation == "move_callback_to_dead_letter"
    assert dead_letter_match.endpoint.auth_required is True
    assert dead_letter_match.path_params == {"delivery_id": "del-1"}


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


def test_server_health_validates_check_records_before_publication() -> None:
    with pytest.raises(ValueError, match="server health service must not be empty"):
        ServerHealth(service=" ")
    with pytest.raises(ValueError, match="server health observed_at must not be empty"):
        ServerHealth(service="graphblocks-api", observed_at=" ")
    with pytest.raises(ValueError, match="server health checks must be a collection of check records"):
        ServerHealth(service="graphblocks-api", checks=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server health check records must contain name, status, and details"):
        ServerHealth(service="graphblocks-api", checks=(("runtime", "healthy"),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server health check name must not be empty"):
        ServerHealth(service="graphblocks-api", checks=((" ", "healthy", {}),))
    with pytest.raises(ValueError, match="invalid server health status offline"):
        ServerHealth(service="graphblocks-api", checks=(("runtime", "offline", {}),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server health check details must be a mapping"):
        ServerHealth(service="graphblocks-api", checks=(("runtime", "healthy", object()),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server health check detail keys must be non-empty strings"):
        ServerHealth(service="graphblocks-api", checks=(("runtime", "healthy", {object(): 1}),))  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="server health check detail keys must be non-empty strings"):
        ServerHealth(service="graphblocks-api", checks=(("runtime", "healthy", {" ": 1}),))


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
    with pytest.raises(ValueError, match="server response JSON payload must be a mapping"):
        ServerResponse.json(200, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server response JSON payload must be a mapping"):
        ServerResponse.json(200, [("ok", True)])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server response JSON payload keys must be non-empty strings"):
        ServerResponse.json(200, {object(): True})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="server response JSON payload keys must be non-empty strings"):
        ServerResponse.json(200, {" ": True})
    with pytest.raises(ValueError, match="server response JSON payload.score must be finite"):
        ServerResponse.json(200, {"score": math.nan})
    with pytest.raises(ValueError, match="server response JSON payload.nested.score must be finite"):
        ServerResponse.json(200, {"nested": {"score": math.inf}})
    with pytest.raises(ValueError, match="server response JSON payload.nested keys must be non-empty strings"):
        ServerResponse.json(200, {"nested": {1: "coerced"}})  # type: ignore[dict-item]


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
    with pytest.raises(ValueError, match="application protocol capabilities commands must be a sequence"):
        ApplicationProtocolCapabilities("graphblocks.app.v1").with_commands("InvokeGraph")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="application protocol capabilities commands must not be empty"):
        ApplicationProtocolCapabilities("graphblocks.app.v1").with_commands([" "])
    with pytest.raises(ValueError, match="application protocol capabilities events must be a sequence"):
        ApplicationProtocolCapabilities("graphblocks.app.v1").with_events(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="application protocol negotiation peer must be ApplicationProtocolCapabilities"):
        server.negotiate(object())  # type: ignore[arg-type]


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


def test_server_app_rejects_non_standard_request_json_constants() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=b'{"graph": NaN}',
            requested_at="2026-06-24T00:00:02Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run request body must be valid JSON",
    }


def test_server_app_accepted_invoke_returns_replayable_run_handle() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-accepted-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Accepted {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    response = app.handle(
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
                    "runId": "run-accepted-1",
                    "responseId": "response-accepted-1",
                    "releaseId": "release-accepted-1",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-accepted-1",
        "status": "accepted",
        "eventStream": "/runs/run-accepted-1/events",
        "websocket": "/runs/run-accepted-1/ws",
        "cancel": "/runs/run-accepted-1/cancel",
        "initialCursor": "run-accepted-1:0",
    }

    attach = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-accepted-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": "run-accepted-1:0"}).encode("utf-8"),
        )
    )

    attach_payload = json.loads(attach.body.decode("utf-8"))
    assert attach.status_code == 200
    assert attach_payload["lastCursor"] == "run-accepted-1:2"
    assert [event["kind"] for event in attach_payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert attach_payload["events"][0]["metadata"]["cursor"] == "run-accepted-1:1"
    assert attach_payload["events"][0]["metadata"]["visibility"] == "client"
    assert attach_payload["events"][1]["metadata"]["cursor"] == "run-accepted-1:2"
    assert attach_payload["events"][1]["metadata"]["visibility"] == "client"
    assert attach_payload["events"][1]["payload"]["outputs"] == {"prompt": "Accepted ok"}


def test_server_app_accepted_invoke_encodes_run_handle_route_links() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-accepted-encoded-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Accepted encoded {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    response = app.handle(
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
                    "runId": "run/accepted?query#fragment",
                    "responseId": "response-accepted-encoded-1",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run/accepted?query#fragment",
        "status": "accepted",
        "eventStream": "/runs/run%2Faccepted%3Fquery%23fragment/events",
        "websocket": "/runs/run%2Faccepted%3Fquery%23fragment/ws",
        "cancel": "/runs/run%2Faccepted%3Fquery%23fragment/cancel",
        "initialCursor": "run/accepted?query#fragment:0",
    }


def test_server_app_rejects_duplicate_invoke_run_id_without_overwriting_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-duplicate-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Duplicate {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "first"}},
                    "runId": "run-duplicate-1",
                    "responseId": "response-duplicate-first",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "second"}},
                    "runId": "run-duplicate-1",
                    "responseId": "response-duplicate-second",
                    "occurredAt": "2026-07-02T00:00:01Z",
                }
            ).encode("utf-8"),
        )
    )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-duplicate-1",
        "error": "run 'run-duplicate-1' already exists",
    }

    attach = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-duplicate-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": "run-duplicate-1:0"}).encode("utf-8"),
        )
    )
    attach_payload = json.loads(attach.body.decode("utf-8"))
    assert attach.status_code == 200
    assert attach_payload["events"][0]["metadata"]["responseId"] == "response-duplicate-first"
    assert attach_payload["events"][1]["payload"]["outputs"] == {"prompt": "Duplicate first"}


def test_server_app_rejects_invoke_graph_with_invalid_occurred_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-invalid-occurred-at"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Invalid {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    response = app.handle(
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
                    "runId": "run-invalid-occurred-at-1",
                    "responseId": "response-invalid-occurred-at-1",
                    "occurredAt": "not-a-date",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run request occurredAt must be an ISO datetime",
    }
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-invalid-occurred-at-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    assert status.status_code == 404

    compact_offset = app.handle(
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
                    "runId": "run-invalid-occurred-at-compact-offset",
                    "responseId": "response-invalid-occurred-at-compact-offset",
                    "occurredAt": "2026-07-02T00:00:00+0000",
                }
            ).encode("utf-8"),
        )
    )

    assert compact_offset.status_code == 400
    assert json.loads(compact_offset.body.decode("utf-8")) == {
        "ok": False,
        "error": "run request occurredAt must be an ISO datetime",
    }


def test_server_app_handles_authenticated_cancel_request() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-server-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-server-1"},
            "metadata": {
                "runId": "run-server-1",
                "sequence": 1,
                "cursor": "run-server-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-06-24T00:00:02Z",
            },
        },
    )

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
        "status": "cancelled",
        "reason": None,
        "lastCursor": "run-server-1:1",
    }
    assert app.run_controls("run-server-1") == (
        {
            "operation": "cancel_run",
            "status": "cancelled",
            "reason": None,
            "occurredAt": "2026-06-24T00:00:03Z",
            "lastCursor": "run-server-1:1",
            "actor": {
                "principalId": "user-1",
                "tenantId": None,
                "groups": (),
                "roles": (),
                "attributes": {},
            },
        },
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-server-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:00:04Z",
        )
    )
    status_payload = json.loads(status.body.decode("utf-8"))
    assert status_payload["state"] == "cancelled"
    assert status_payload["completedAt"] == "2026-06-24T00:00:03Z"


def test_server_app_rejects_run_control_after_terminal_event_stream() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    for suffix, terminal_kind, terminal_state in (
        ("succeeded", "RunSucceeded", "succeeded"),
        ("completed", "RunCompleted", "completed"),
        ("failed", "RunFailed", "failed"),
        ("cancelled", "RunCancelled", "cancelled"),
        ("policy-stopped", "RunPolicyStopped", "policy_stopped"),
        ("expired", "RunExpired", "expired"),
    ):
        run_id = f"run-terminal-control-{suffix}"
        app._events_by_run_id[run_id] = (
            {
                "kind": "RunStarted",
                "payload": {"runId": run_id},
                "metadata": {
                    "runId": run_id,
                    "sequence": 1,
                    "cursor": f"{run_id}:1",
                    "releaseId": "release-terminal-control-1",
                    "occurredAt": "2026-06-24T00:00:01Z",
                },
            },
            {
                "kind": terminal_kind,
                "payload": {"status": terminal_state, "outputs": {}},
                "metadata": {
                    "runId": run_id,
                    "sequence": 2,
                    "cursor": f"{run_id}:2",
                    "releaseId": "release-terminal-control-1",
                    "occurredAt": "2026-06-24T00:00:02Z",
                },
            },
        )

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/runs/{run_id}/resume",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                requested_at="2026-06-24T00:00:03Z",
            )
        )

        assert response.status_code == 409
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "runId": run_id,
            "state": terminal_state,
            "error": f"run {run_id} is terminal with state {terminal_state}",
        }
        assert app.run_controls(run_id) == ()


def test_server_app_records_run_control_projection_without_mutating_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-control"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Control {message.text}"},
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
                    "runId": "run-control-1",
                    "responseId": "response-control-1",
                    "releaseId": "release-control-1",
                    "policySnapshotId": "policy-control-1",
                    "occurredAt": "2026-06-24T00:01:00Z",
                }
            ).encode("utf-8"),
            requested_at="2026-06-24T00:01:00Z",
        )
    )
    assert run.status_code == 200
    app._events_by_run_id["run-control-1"] = app._events_by_run_id["run-control-1"][:1]

    pause = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="2026-06-24T00:01:01Z",
        )
    )
    paused_status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-control-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:01:02Z",
        )
    )
    resume = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:01:03Z",
        )
    )
    expire = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-1/expire",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "retention_deadline"}).encode("utf-8"),
            requested_at="2026-06-24T00:01:04Z",
        )
    )
    expired_status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-control-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:01:05Z",
        )
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-control-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:01:06Z",
        )
    )

    assert pause.status_code == 202
    assert json.loads(pause.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-control-1",
        "status": "paused_operator",
        "reason": "operator_hold",
        "lastCursor": "run-control-1:1",
    }
    paused_payload = json.loads(paused_status.body.decode("utf-8"))
    assert paused_payload["state"] == "paused_operator"
    assert paused_payload["waitingOn"] == [{"kind": "operator", "reason": "operator_hold"}]
    assert json.loads(resume.body.decode("utf-8"))["status"] == "resuming"
    assert json.loads(expire.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-control-1",
        "status": "expired",
        "reason": "retention_deadline",
        "lastCursor": "run-control-1:1",
    }
    expired_payload = json.loads(expired_status.body.decode("utf-8"))
    assert expired_payload["state"] == "expired"
    assert expired_payload["completedAt"] == "2026-06-24T00:01:04Z"
    event_payload = json.loads(events.body.decode("utf-8"))
    assert [event["kind"] for event in event_payload["events"]] == ["RunStarted"]
    assert app.run_controls("run-control-1") == (
        {
            "operation": "pause_run",
            "status": "paused_operator",
            "reason": "operator_hold",
            "occurredAt": "2026-06-24T00:01:01Z",
            "lastCursor": "run-control-1:1",
            "actor": {
                "principalId": "user-1",
                "tenantId": None,
                "groups": (),
                "roles": (),
                "attributes": {},
            },
        },
        {
            "operation": "resume_run",
            "status": "resuming",
            "reason": None,
            "occurredAt": "2026-06-24T00:01:03Z",
            "lastCursor": "run-control-1:1",
            "actor": {
                "principalId": "user-1",
                "tenantId": None,
                "groups": (),
                "roles": (),
                "attributes": {},
            },
        },
        {
            "operation": "expire_run",
            "status": "expired",
            "reason": "retention_deadline",
            "occurredAt": "2026-06-24T00:01:04Z",
            "lastCursor": "run-control-1:1",
            "actor": {
                "principalId": "user-1",
                "tenantId": None,
                "groups": (),
                "roles": (),
                "attributes": {},
            },
        },
    )
    with pytest.raises(TypeError):
        app.run_controls("run-control-1")[0]["reason"] = "changed"


def test_server_app_rejects_run_control_for_missing_stream_or_malformed_reason() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    missing = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/missing-run/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-06-24T00:01:07Z",
        )
    )

    assert missing.status_code == 404
    assert json.loads(missing.body.decode("utf-8")) == {
        "ok": False,
        "error": "run control stream not found for run 'missing-run'",
    }

    app._events_by_run_id["run-control-invalid-1"] = ()
    invalid = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-invalid-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": ""}).encode("utf-8"),
            requested_at="2026-06-24T00:01:08Z",
        )
    )

    assert invalid.status_code == 400
    assert json.loads(invalid.body.decode("utf-8")) == {
        "ok": False,
        "error": "run control request reason must not be empty",
    }


def test_server_app_rejects_run_control_with_whitespace_wrapped_reason() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-control-whitespace-reason-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-control-whitespace-reason-1"},
            "metadata": {
                "runId": "run-control-whitespace-reason-1",
                "sequence": 1,
                "cursor": "run-control-whitespace-reason-1:1",
                "releaseId": "release-control-whitespace-reason-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-whitespace-reason-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": " operator_hold"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run control request reason must not contain surrounding whitespace",
    }
    assert app.run_controls("run-control-whitespace-reason-1") == ()


def test_server_app_rejects_run_control_with_invalid_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-control-invalid-time-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-control-invalid-time-1"},
            "metadata": {
                "runId": "run-control-invalid-time-1",
                "sequence": 1,
                "cursor": "run-control-invalid-time-1:1",
                "releaseId": "release-control-invalid-time-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-invalid-time-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run control request occurred_at must be an ISO datetime",
    }
    assert app.run_controls("run-control-invalid-time-1") == ()


def test_server_app_rejects_run_control_duplicate_with_conflicting_reason() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-control-duplicate-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-control-duplicate-1"},
            "metadata": {
                "runId": "run-control-duplicate-1",
                "sequence": 1,
                "cursor": "run-control-duplicate-1:1",
                "releaseId": "release-control-duplicate-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-duplicate-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-duplicate-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )
    conflict = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-control-duplicate-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "different_hold"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:03Z",
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert conflict.status_code == 409
    assert json.loads(conflict.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-control-duplicate-1",
        "status": "paused_operator",
        "reason": "operator_hold",
        "requestedReason": "different_hold",
        "error": "run control duplicate command conflicts with existing reason",
    }
    assert len(app.run_controls("run-control-duplicate-1")) == 1


def test_server_app_projects_typed_pause_wait_reason() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-pause-kind-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-pause-kind-1"},
            "metadata": {
                "runId": "run-pause-kind-1",
                "sequence": 1,
                "cursor": "run-pause-kind-1:1",
                "releaseId": "release-pause-kind-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )

    pause = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-pause-kind-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"pauseKind": "budget", "reason": "quota_exhausted"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-pause-kind-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:02Z",
        )
    )

    assert pause.status_code == 202
    assert json.loads(pause.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-pause-kind-1",
        "status": "paused_budget",
        "reason": "quota_exhausted",
        "lastCursor": "run-pause-kind-1:1",
    }
    status_payload = json.loads(status.body.decode("utf-8"))
    assert status_payload["state"] == "paused_budget"
    assert status_payload["waitingOn"] == [{"kind": "budget", "reason": "quota_exhausted"}]
    assert app.run_controls("run-pause-kind-1")[0]["status"] == "paused_budget"


def test_server_app_rejects_unknown_pause_kind() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-pause-kind-invalid-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-pause-kind-invalid-1"},
            "metadata": {
                "runId": "run-pause-kind-invalid-1",
                "sequence": 1,
                "cursor": "run-pause-kind-invalid-1:1",
                "releaseId": "release-pause-kind-invalid-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-pause-kind-invalid-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"pauseKind": "network"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run control request pauseKind must be one of operator, budget, policy, or callback_delivery",
    }
    assert app.run_controls("run-pause-kind-invalid-1") == ()


def test_server_app_rejects_non_terminal_control_after_terminal_run_state() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-terminal-control-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-terminal-control-1"},
            "metadata": {
                "runId": "run-terminal-control-1",
                "sequence": 1,
                "cursor": "run-terminal-control-1:1",
                "releaseId": "release-terminal-control-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )
    cancelled = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-terminal-control-1/cancel",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    resume = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-terminal-control-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:02Z",
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-terminal-control-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:03Z",
        )
    )

    assert cancelled.status_code == 202
    assert resume.status_code == 409
    assert json.loads(resume.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-terminal-control-1",
        "state": "cancelled",
        "error": "run run-terminal-control-1 is terminal with state cancelled",
    }
    assert json.loads(status.body.decode("utf-8"))["state"] == "cancelled"
    assert [control["status"] for control in app.run_controls("run-terminal-control-1")] == ["cancelled"]


def test_server_app_treats_repeated_terminal_control_as_idempotent() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-terminal-idempotent-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-terminal-idempotent-1"},
            "metadata": {
                "runId": "run-terminal-idempotent-1",
                "sequence": 1,
                "cursor": "run-terminal-idempotent-1:1",
                "releaseId": "release-terminal-idempotent-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )
    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-terminal-idempotent-1/cancel",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-terminal-idempotent-1/cancel",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:02Z",
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-terminal-idempotent-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:03Z",
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-terminal-idempotent-1",
        "status": "cancelled",
        "reason": None,
        "lastCursor": "run-terminal-idempotent-1:1",
        "duplicate": True,
    }
    assert json.loads(status.body.decode("utf-8"))["state"] == "cancelled"
    assert [control["status"] for control in app.run_controls("run-terminal-idempotent-1")] == ["cancelled"]


def test_server_app_rejects_resume_without_paused_or_waiting_state() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-resume-active-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-resume-active-1"},
            "metadata": {
                "runId": "run-resume-active-1",
                "sequence": 1,
                "cursor": "run-resume-active-1:1",
                "releaseId": "release-resume-active-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-resume-active-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:01Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-resume-active-1",
        "state": "running",
        "error": "run run-resume-active-1 is not paused or waiting and cannot be resumed",
    }
    assert app.run_controls("run-resume-active-1") == ()


def test_server_app_resume_clears_waiting_callback_status_projection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-resume-callback-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-resume-callback-1"},
            "metadata": {
                "runId": "run-resume-callback-1",
                "sequence": 1,
                "cursor": "run-resume-callback-1:1",
                "releaseId": "release-resume-callback-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )
    callback = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-resume-callback-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-resume-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-resume-callback-1",
                    "run_id": "run-resume-callback-1",
                    "node_id": "waitCI",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    resume = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-resume-callback-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:02Z",
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-resume-callback-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:03Z",
        )
    )

    assert callback.status_code == 202
    assert resume.status_code == 202
    status_payload = json.loads(status.body.decode("utf-8"))
    assert status_payload["state"] == "resuming"
    assert status_payload["waitingOn"] == []
    assert status_payload["activeOperations"] == ["op-resume-callback-1"]


def test_server_app_treats_repeated_non_terminal_control_as_idempotent() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-non-terminal-idempotent-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-non-terminal-idempotent-1"},
            "metadata": {
                "runId": "run-non-terminal-idempotent-1",
                "sequence": 1,
                "cursor": "run-non-terminal-idempotent-1:1",
                "releaseId": "release-non-terminal-idempotent-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )
    pause = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-non-terminal-idempotent-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    duplicate_pause = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-non-terminal-idempotent-1/pause",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator_hold"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:02Z",
        )
    )
    resume = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-non-terminal-idempotent-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:03Z",
        )
    )
    duplicate_resume = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-non-terminal-idempotent-1/resume",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:04Z",
        )
    )

    assert pause.status_code == 202
    assert duplicate_pause.status_code == 200
    assert json.loads(duplicate_pause.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-non-terminal-idempotent-1",
        "status": "paused_operator",
        "reason": "operator_hold",
        "lastCursor": "run-non-terminal-idempotent-1:1",
        "duplicate": True,
    }
    assert resume.status_code == 202
    assert duplicate_resume.status_code == 200
    assert json.loads(duplicate_resume.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-non-terminal-idempotent-1",
        "status": "resuming",
        "reason": None,
        "lastCursor": "run-non-terminal-idempotent-1:1",
        "duplicate": True,
    }
    assert [control["status"] for control in app.run_controls("run-non-terminal-idempotent-1")] == [
        "paused_operator",
        "resuming",
    ]


def test_server_app_accepts_authenticated_async_callback_submission() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay", roles=("operator",))})
    )
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "policySnapshotId": "policy-callback-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    payload_digest = graphblocks.canonical_hash({"status": "completed"})

    assert response.status_code == 202
    assert payload == {
        "ok": True,
        "operationId": "op-ci-1",
        "callbackId": "cb-1",
        "idempotencyKey": "idem-callback-1",
        "payloadDigest": payload_digest,
        "verifiedBy": "callback-relay",
        "policySnapshotId": "policy-callback-1",
        "runId": "run-1",
        "nodeId": "waitCI",
        "attemptId": "attempt-1",
        "status": "accepted",
    }
    assert app.callback_submissions("op-ci-1") == (
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest=payload_digest,
            run_id="run-1",
            node_id="waitCI",
            attempt_id="attempt-1",
            provider_operation_id=None,
            received_at="2026-07-02T00:00:00Z",
            verified_by="callback-relay",
            policy_snapshot_id="policy-callback-1",
        ),
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    status_payload = json.loads(status.body.decode("utf-8"))

    assert status_payload["state"] == "waiting_callback"
    assert status_payload["waitingOn"] == [
        {
            "kind": "callback",
            "operationId": "op-ci-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
        }
    ]
    assert status_payload["activeOperations"] == ["op-ci-1"]
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-1:1"},
            cookies={},
        )
    )
    event_payload = json.loads(events.body.decode("utf-8"))
    assert events.status_code == 200
    assert event_payload["lastCursor"] == "run-1:2"
    assert [event["kind"] for event in event_payload["events"]] == ["ExternalCallbackReceived"]
    received_event = event_payload["events"][0]
    assert received_event["metadata"]["sequence"] == 2
    assert received_event["metadata"]["cursor"] == "run-1:2"
    assert received_event["metadata"]["operationId"] == "op-ci-1"
    assert received_event["metadata"]["nodeId"] == "waitCI"
    assert received_event["metadata"]["visibility"] == "operator"
    assert received_event["payload"] == {
        "callbackId": "cb-1",
        "idempotencyKey": "idem-callback-1",
        "payloadDigest": payload_digest,
        "verifiedBy": "callback-relay",
        "policySnapshotId": "policy-callback-1",
        "attemptId": "attempt-1",
        "receivedAt": "2026-07-02T00:00:00Z",
    }


def test_server_app_rejects_async_callback_operation_id_mismatch() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-path",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-mismatch"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "operationId": "op-ci-body",
                    "callback_id": "cb-mismatch",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server async callback operation_id must match callback endpoint operation_id",
    }
    assert app.callback_submissions("op-ci-path") == ()
    assert app.callback_submissions("op-ci-body") == ()
    assert app.async_callback_rejections("op-ci-path") == (
        {
            "operationId": "op-ci-path",
            "callbackId": "cb-mismatch",
            "idempotencyKey": "idem-callback-mismatch",
            **_callback_rejection_metadata({"status": "completed"}),
            "attemptId": "attempt-1",
            "reason": "operation_id_mismatch",
            "receivedAt": "2026-07-03T00:00:00Z",
        },
    )


def test_server_app_rejects_async_callback_when_required_authentication_is_unconfigured() -> None:
    app = GraphBlocksServerApp(require_async_callback_authentication=True)

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-auth-required",
            headers={"GraphBlocks-Idempotency-Key": "idem-callback-auth-required"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-auth-required",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 401
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "reasonCodes": ["auth.callback_authentication_required"],
    }
    assert app.callback_submissions("op-ci-auth-required") == ()


def test_server_app_records_async_callback_authentication_failure_rejection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-auth-failed",
            headers={
                "Authorization": "Bearer wrong-token",
                "GraphBlocks-Idempotency-Key": "idem-callback-auth-failed",
            },
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-auth-failed",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 401
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "reasonCodes": ["auth.invalid_bearer_token"],
    }
    assert app.callback_submissions("op-ci-auth-failed") == ()
    assert app.async_callback_rejections("op-ci-auth-failed") == (
        {
            "operationId": "op-ci-auth-failed",
            "callbackId": "cb-auth-failed",
            "idempotencyKey": "idem-callback-auth-failed",
            **_callback_rejection_metadata({"status": "completed"}, verified_by="unauthenticated"),
            "attemptId": "attempt-1",
            "reason": "authentication_failed",
            "receivedAt": "2026-07-03T00:00:00Z",
        },
    )


def test_server_app_validates_async_callback_authentication_requirement_flag() -> None:
    with pytest.raises(ValueError, match="server require_async_callback_authentication must be a boolean"):
        GraphBlocksServerApp(require_async_callback_authentication="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server anti_enumerate_async_callbacks must be a boolean"):
        GraphBlocksServerApp(anti_enumerate_async_callbacks="yes")  # type: ignore[arg-type]


def test_server_app_terminal_run_status_suppresses_active_callback_waits() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-1/cancel",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:02Z",
        )
    )

    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            requested_at="2026-07-02T00:00:03Z",
        )
    )
    status_payload = json.loads(status.body.decode("utf-8"))

    assert status_payload["state"] == "cancelled"
    assert status_payload["completedAt"] == "2026-07-02T00:00:02Z"
    assert status_payload["waitingOn"] == []
    assert status_payload["activeOperations"] == []


def test_server_app_rejects_async_callback_for_unknown_declared_run() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unknown-run",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-unknown-run"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-unknown-run",
                    "attempt_id": "attempt-1",
                    "run_id": "missing-run",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-unknown-run",
        "runId": "missing-run",
        "error": "async callback run 'missing-run' not found",
    }
    assert app.callback_submissions("op-ci-unknown-run") == ()
    assert app.async_callback_rejections("op-ci-unknown-run") == (
        {
            "operationId": "op-ci-unknown-run",
            "callbackId": "cb-unknown-run",
            "idempotencyKey": "idem-callback-unknown-run",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "missing-run",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "unknown_run",
            "receivedAt": "2026-07-03T00:00:00Z",
        },
    )


def test_server_app_can_anti_enumerate_unknown_declared_async_callback_run() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}),
        anti_enumerate_async_callbacks=True,
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unknown-run",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-unknown-run"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-unknown-run",
                    "attempt_id": "attempt-1",
                    "run_id": "missing-run",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "status": "accepted",
    }
    assert app.callback_submissions("op-ci-unknown-run") == ()
    assert app.async_callback_rejections("op-ci-unknown-run") == (
        {
            "operationId": "op-ci-unknown-run",
            "callbackId": "cb-unknown-run",
            "idempotencyKey": "idem-callback-unknown-run",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "missing-run",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "unknown_run",
            "receivedAt": "2026-07-03T00:00:00Z",
        },
    )


def test_server_app_rejects_async_callback_declared_run_without_attempt_fence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    for index, body in enumerate(
        (
            {
                "callback_id": "cb-fence-1",
                "run_id": "run-callback-fence-1",
                "node_id": "waitCI",
                "payload": {"status": "completed"},
            },
            {
                "callbackId": "cb-fence-2",
                "runId": "run-callback-fence-2",
                "nodeId": "waitCI",
                "providerOperationId": "provider-ci-2",
                "payload": {"status": "completed", "checks": [{"name": "unit", "passed": True}]},
            },
            {
                "callback_id": "cb-fence-3",
                "runId": "run-callback-fence-3",
                "payload": {"status": "failed", "diagnostics": [{"message": "compile failed"}]},
            },
        ),
        start=1,
    ):
        run_id = body.get("run_id", body.get("runId"))
        assert isinstance(run_id, str)
        app._events_by_run_id[run_id] = (
            {
                "kind": "RunStarted",
                "payload": {"runId": run_id},
                "metadata": {
                    "runId": run_id,
                    "sequence": 1,
                    "cursor": f"{run_id}:1",
                    "releaseId": "release-callback-fence-1",
                    "occurredAt": "2026-07-03T00:00:00Z",
                },
            },
        )
        operation_id = f"op-ci-fence-{index}"

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/callbacks/{operation_id}",
                headers={
                    "Authorization": "Bearer token-1",
                    "GraphBlocks-Idempotency-Key": f"idem-callback-fence-{index}",
                },
                query={},
                cookies={},
                body=json.dumps(body).encode("utf-8"),
                requested_at="2026-07-03T00:00:01Z",
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "operationId": operation_id,
            "runId": run_id,
            "error": "async callback attempt_id is required when run_id is declared",
        }
        assert app.callback_submissions(operation_id) == ()
        expected_rejection = {
            "operationId": operation_id,
            "callbackId": f"cb-fence-{index}",
            "idempotencyKey": f"idem-callback-fence-{index}",
            **_callback_rejection_metadata(body["payload"]),  # type: ignore[arg-type]
            "runId": run_id,
            "reason": "missing_attempt_fence",
            "receivedAt": "2026-07-03T00:00:01Z",
        }
        node_id = body.get("node_id", body.get("nodeId"))
        if node_id is not None:
            expected_rejection["nodeId"] = node_id
        provider_operation_id = body.get("provider_operation_id", body.get("providerOperationId"))
        if provider_operation_id is not None:
            expected_rejection["providerOperationId"] = provider_operation_id
        assert app.async_callback_rejections(operation_id) == (expected_rejection,)


def test_server_app_rejects_async_callback_declared_run_without_node_fence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-callback-node-fence-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-callback-node-fence-1"},
            "metadata": {
                "runId": "run-callback-node-fence-1",
                "sequence": 1,
                "cursor": "run-callback-node-fence-1:1",
                "releaseId": "release-callback-node-fence-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-node-fence-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-node-fence"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-node-fence",
                    "attempt_id": "attempt-1",
                    "run_id": "run-callback-node-fence-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-node-fence-1",
        "runId": "run-callback-node-fence-1",
        "error": "async callback node_id is required when run_id is declared",
    }
    assert app.callback_submissions("op-ci-node-fence-1") == ()
    assert app.async_callback_rejections("op-ci-node-fence-1") == (
        {
            "operationId": "op-ci-node-fence-1",
            "callbackId": "cb-node-fence",
            "idempotencyKey": "idem-callback-node-fence",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-callback-node-fence-1",
            "attemptId": "attempt-1",
            "reason": "missing_node_fence",
            "receivedAt": "2026-07-03T00:00:01Z",
        },
    )


def test_server_app_deduplicates_async_callback_submission_by_idempotency_key() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    request = ServerRequest(
        method="POST",
        path="/callbacks/op-ci-1",
        headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
        query={},
        cookies={},
        body=json.dumps(
            {
                "callback_id": "cb-1",
                "attempt_id": "attempt-1",
                "payload": {"status": "completed"},
            }
        ).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    first = app.handle(request)
    duplicate = app.handle(request)
    payload_digest = graphblocks.canonical_hash({"status": "completed"})

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "operationId": "op-ci-1",
        "callbackId": "cb-1",
        "idempotencyKey": "idem-callback-1",
        "payloadDigest": payload_digest,
        "verifiedBy": "callback-relay",
        "policySnapshotId": "local",
        "attemptId": "attempt-1",
        "status": "duplicate",
        "duplicate": True,
    }
    assert len(app.callback_submissions("op-ci-1")) == 1


def test_server_app_rejects_conflicting_async_callback_idempotency_replay() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    conflict = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-2",
                    "attempt_id": "attempt-1",
                    "providerOperationId": "provider-ci-retry-1",
                    "payload": {"status": "failed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert json.loads(conflict.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "idempotencyKey": "idem-callback-1",
        "error": "async callback idempotency key was reused with different content",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    rejected_payload_digest = graphblocks.canonical_hash({"status": "failed"})
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-2",
            "idempotencyKey": "idem-callback-1",
            "payloadDigest": rejected_payload_digest,
            "verifiedBy": "callback-relay",
            "policySnapshotId": "local",
            "attemptId": "attempt-1",
            "providerOperationId": "provider-ci-retry-1",
            "reason": "idempotency_conflict",
            "receivedAt": "2026-07-02T00:00:01Z",
        },
    )


def test_server_app_rejection_records_callback_artifact_ids() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                    "artifacts": [{"artifact_id": "artifact-ci-log-1", "uri": "blob://ci/log-1"}],
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    conflict = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                    "artifacts": [{"artifact_id": "artifact-ci-log-2", "uri": "blob://ci/log-2"}],
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:01Z",
        )
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-1",
            "idempotencyKey": "idem-callback-1",
            "payloadDigest": graphblocks.canonical_hash({"status": "completed"}),
            "verifiedBy": "callback-relay",
            "policySnapshotId": "local",
            "attemptId": "attempt-1",
            "artifactIds": ["artifact-ci-log-2"],
            "reason": "idempotency_conflict",
            "receivedAt": "2026-07-02T00:00:01Z",
        },
    )


def test_server_app_rejects_stale_async_callback_attempt_for_existing_operation() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay", roles=("operator",))})
    )
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    current = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-current"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-current",
                    "attempt_id": "attempt-2",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    stale = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-stale"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-stale",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert current.status_code == 202
    assert stale.status_code == 409
    assert json.loads(stale.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "runId": "run-1",
        "attemptId": "attempt-1",
        "error": "async callback operation is already bound to a different run attempt",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    assert app.callback_submissions("op-ci-1")[0].attempt_id == "attempt-2"
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-stale",
            "idempotencyKey": "idem-callback-stale",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "stale_attempt",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-1:2"},
            cookies={},
        )
    )
    event_payload = json.loads(events.body.decode("utf-8"))
    assert events.status_code == 200
    assert event_payload["lastCursor"] == "run-1:3"
    assert [event["kind"] for event in event_payload["events"]] == ["ExternalCallbackRejected"]
    rejection_event = event_payload["events"][0]
    assert rejection_event["metadata"]["sequence"] == 3
    assert rejection_event["metadata"]["cursor"] == "run-1:3"
    assert rejection_event["metadata"]["operationId"] == "op-ci-1"
    assert rejection_event["metadata"]["nodeId"] == "waitCI"
    assert rejection_event["metadata"]["visibility"] == "operator"
    assert rejection_event["payload"] == {
        "callbackId": "cb-stale",
        "idempotencyKey": "idem-callback-stale",
        "payloadDigest": graphblocks.canonical_hash({"status": "completed"}),
        "verifiedBy": "callback-relay",
        "policySnapshotId": "local",
        "attemptId": "attempt-1",
        "reason": "stale_attempt",
        "receivedAt": "2026-07-03T00:00:02Z",
    }


def test_server_app_rejects_async_callback_for_different_node_on_existing_run_attempt() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    current = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-current"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-current",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    wrong_node = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-wrong-node"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-wrong-node",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "otherWait",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert current.status_code == 202
    assert wrong_node.status_code == 409
    assert json.loads(wrong_node.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "runId": "run-1",
        "attemptId": "attempt-1",
        "nodeId": "otherWait",
        "error": "async callback operation is already bound to a different run node attempt",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    assert app.callback_submissions("op-ci-1")[0].node_id == "waitCI"
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-wrong-node",
            "idempotencyKey": "idem-callback-wrong-node",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-1",
            "nodeId": "otherWait",
            "attemptId": "attempt-1",
            "reason": "node_mismatch",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )


def test_server_app_rejects_async_callback_for_different_run_on_existing_operation_attempt() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    for run_id in ("run-1", "run-2"):
        app._events_by_run_id[run_id] = (
            {
                "kind": "RunStarted",
                "payload": {"runId": run_id},
                "metadata": {
                    "runId": run_id,
                    "sequence": 1,
                    "cursor": f"{run_id}:1",
                    "releaseId": "release-1",
                    "occurredAt": "2026-07-03T00:00:00Z",
                },
            },
        )

    current = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-current"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-current",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    wrong_run = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-wrong-run"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-wrong-run",
                    "attempt_id": "attempt-1",
                    "run_id": "run-2",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert current.status_code == 202
    assert wrong_run.status_code == 409
    assert json.loads(wrong_run.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "runId": "run-2",
        "attemptId": "attempt-1",
        "error": "async callback operation is already bound to a different run attempt",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    assert app.callback_submissions("op-ci-1")[0].run_id == "run-1"
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-wrong-run",
            "idempotencyKey": "idem-callback-wrong-run",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-2",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "stale_attempt",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )


def test_server_app_rejects_second_async_callback_for_same_operation_attempt() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed", "sequence": 1},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    second = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-2"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-2",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed", "sequence": 2},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert json.loads(second.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "runId": "run-1",
        "attemptId": "attempt-1",
        "nodeId": "waitCI",
        "error": "async callback operation already has a recorded receipt",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    assert app.callback_submissions("op-ci-1")[0].callback_id == "cb-1"
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-2",
            "idempotencyKey": "idem-callback-2",
            **_callback_rejection_metadata({"status": "completed", "sequence": 2}),
            "runId": "run-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "duplicate_operation_receipt",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )


def test_server_app_rejects_async_callback_provider_operation_mismatch() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-1",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "providerOperationId": "provider-ci-1",
                    "payload": {"status": "completed", "sequence": 1},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    wrong_provider = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-2"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-2",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "providerOperationId": "provider-ci-2",
                    "payload": {"status": "completed", "sequence": 2},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert first.status_code == 202
    assert wrong_provider.status_code == 409
    assert json.loads(wrong_provider.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-1",
        "runId": "run-1",
        "attemptId": "attempt-1",
        "nodeId": "waitCI",
        "providerOperationId": "provider-ci-2",
        "error": "async callback operation is already bound to a different provider operation",
    }
    assert len(app.callback_submissions("op-ci-1")) == 1
    assert app.callback_submissions("op-ci-1")[0].provider_operation_id == "provider-ci-1"
    assert app.async_callback_rejections("op-ci-1") == (
        {
            "operationId": "op-ci-1",
            "callbackId": "cb-2",
            "idempotencyKey": "idem-callback-2",
            **_callback_rejection_metadata({"status": "completed", "sequence": 2}),
            "runId": "run-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "providerOperationId": "provider-ci-2",
            "reason": "provider_operation_mismatch",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )


def test_server_app_deterministic_callback_race_permutations_keep_first_receipt_authoritative() -> None:
    trailing_callbacks = (
        {
            "label": "exact_replay",
            "callback_id": "cb-1",
            "idempotency_key": "idem-callback-1",
            "attempt_id": "attempt-1",
            "node_id": "waitCI",
            "payload": {"status": "completed", "sequence": 1},
            "expected_status": 200,
            "expected_reason": None,
        },
        {
            "label": "duplicate_operation",
            "callback_id": "cb-2",
            "idempotency_key": "idem-callback-2",
            "attempt_id": "attempt-1",
            "node_id": "waitCI",
            "payload": {"status": "completed", "sequence": 2},
            "expected_status": 409,
            "expected_reason": "duplicate_operation_receipt",
        },
        {
            "label": "stale_attempt",
            "callback_id": "cb-stale",
            "idempotency_key": "idem-callback-stale",
            "attempt_id": "attempt-0",
            "node_id": "waitCI",
            "payload": {"status": "completed", "sequence": 0},
            "expected_status": 409,
            "expected_reason": "stale_attempt",
        },
        {
            "label": "wrong_node",
            "callback_id": "cb-wrong-node",
            "idempotency_key": "idem-callback-wrong-node",
            "attempt_id": "attempt-1",
            "node_id": "otherWait",
            "payload": {"status": "completed", "sequence": 3},
            "expected_status": 409,
            "expected_reason": "node_mismatch",
        },
    )

    for permutation_index, callback_order in enumerate(permutations(trailing_callbacks), start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
        app._events_by_run_id["run-1"] = (
            {
                "kind": "RunStarted",
                "payload": {"runId": "run-1"},
                "metadata": {
                    "runId": "run-1",
                    "sequence": 1,
                    "cursor": "run-1:1",
                    "releaseId": "release-1",
                    "occurredAt": "2026-07-03T00:00:00Z",
                },
            },
        )

        first = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "attempt_id": "attempt-1",
                        "run_id": "run-1",
                        "node_id": "waitCI",
                        "payload": {"status": "completed", "sequence": 1},
                    }
                ).encode("utf-8"),
                requested_at="2026-07-03T00:00:01Z",
            )
        )
        assert first.status_code == 202, f"permutation {permutation_index}"

        expected_reasons: list[str] = []
        for callback_index, callback in enumerate(callback_order, start=2):
            response = app.handle(
                ServerRequest(
                    method="POST",
                    path="/callbacks/op-ci-1",
                    headers={
                        "Authorization": "Bearer token-1",
                        "GraphBlocks-Idempotency-Key": callback["idempotency_key"],
                    },
                    query={},
                    cookies={},
                    body=json.dumps(
                        {
                            "callback_id": callback["callback_id"],
                            "attempt_id": callback["attempt_id"],
                            "run_id": "run-1",
                            "node_id": callback["node_id"],
                            "payload": callback["payload"],
                        }
                    ).encode("utf-8"),
                    requested_at=f"2026-07-03T00:00:0{callback_index}Z",
                )
            )
            assert response.status_code == callback["expected_status"], callback["label"]
            if callback["expected_reason"] is not None:
                expected_reasons.append(callback["expected_reason"])

        assert len(app.callback_submissions("op-ci-1")) == 1, f"permutation {permutation_index}"
        assert app.callback_submissions("op-ci-1")[0].callback_id == "cb-1"
        assert [
            rejection["reason"] for rejection in app.async_callback_rejections("op-ci-1")
        ] == expected_reasons


def test_server_app_rejects_async_callback_scope_change_for_existing_operation() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    app._events_by_run_id["run-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-1"},
            "metadata": {
                "runId": "run-1",
                "sequence": 1,
                "cursor": "run-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    unscoped_first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unscoped-first",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-unscoped"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-unscoped",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    scoped_after_unscoped = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unscoped-first",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-scoped"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-scoped",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert unscoped_first.status_code == 202
    assert scoped_after_unscoped.status_code == 409
    assert json.loads(scoped_after_unscoped.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-unscoped-first",
        "runId": "run-1",
        "attemptId": "attempt-1",
        "error": "async callback operation scope cannot change after first receipt",
    }
    assert len(app.callback_submissions("op-ci-unscoped-first")) == 1
    assert app.callback_submissions("op-ci-unscoped-first")[0].run_id is None
    assert app.async_callback_rejections("op-ci-unscoped-first") == (
        {
            "operationId": "op-ci-unscoped-first",
            "callbackId": "cb-scoped",
            "idempotencyKey": "idem-callback-scoped",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "reason": "scope_mismatch",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )

    scoped_first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-scoped-first",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-scoped-first"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-scoped-first",
                    "attempt_id": "attempt-1",
                    "run_id": "run-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:03Z",
        )
    )
    unscoped_after_scoped = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-scoped-first",
            headers={
                "Authorization": "Bearer token-1",
                "GraphBlocks-Idempotency-Key": "idem-callback-unscoped-after-scoped",
            },
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-unscoped-after-scoped",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:04Z",
        )
    )

    assert scoped_first.status_code == 202
    assert unscoped_after_scoped.status_code == 409
    assert json.loads(unscoped_after_scoped.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-scoped-first",
        "error": "async callback operation scope cannot change after first receipt",
    }
    assert len(app.callback_submissions("op-ci-scoped-first")) == 1
    assert app.callback_submissions("op-ci-scoped-first")[0].run_id == "run-1"
    assert app.async_callback_rejections("op-ci-scoped-first") == (
        {
            "operationId": "op-ci-scoped-first",
            "callbackId": "cb-unscoped-after-scoped",
            "idempotencyKey": "idem-callback-unscoped-after-scoped",
            **_callback_rejection_metadata({"status": "completed"}),
            "attemptId": "attempt-1",
            "reason": "scope_mismatch",
            "receivedAt": "2026-07-03T00:00:04Z",
        },
    )


def test_server_app_rejects_unscoped_async_callback_attempt_change_for_existing_operation() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unscoped-attempt",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-attempt-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-attempt-1",
                    "attempt_id": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    changed_attempt = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-unscoped-attempt",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-attempt-2"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-attempt-2",
                    "attempt_id": "attempt-2",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert first.status_code == 202
    assert changed_attempt.status_code == 409
    assert json.loads(changed_attempt.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-unscoped-attempt",
        "attemptId": "attempt-2",
        "error": "async callback operation is already bound to a different run attempt",
    }
    assert len(app.callback_submissions("op-ci-unscoped-attempt")) == 1
    assert app.callback_submissions("op-ci-unscoped-attempt")[0].attempt_id == "attempt-1"
    assert app.async_callback_rejections("op-ci-unscoped-attempt") == (
        {
            "operationId": "op-ci-unscoped-attempt",
            "callbackId": "cb-attempt-2",
            "idempotencyKey": "idem-callback-attempt-2",
            **_callback_rejection_metadata({"status": "completed"}),
            "attemptId": "attempt-2",
            "reason": "stale_attempt",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )


def test_server_app_rejects_async_callback_for_terminal_declared_run() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay", roles=("operator",))})
    )
    app._events_by_run_id["run-terminal-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-terminal-1"},
            "metadata": {
                "runId": "run-terminal-1",
                "sequence": 1,
                "cursor": "run-terminal-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
        {
            "kind": "RunCancelled",
            "payload": {"runId": "run-terminal-1", "reason": "client request"},
            "metadata": {
                "runId": "run-terminal-1",
                "sequence": 2,
                "cursor": "run-terminal-1:2",
                "releaseId": "release-1",
                "occurredAt": "2026-07-03T00:00:01Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-terminal-1",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-terminal"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-terminal",
                    "attempt_id": "attempt-1",
                    "run_id": "run-terminal-1",
                    "node_id": "waitCI",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-terminal-1",
        "runId": "run-terminal-1",
        "status": "cancelled",
        "error": "async callback run is terminal and cannot be resumed",
    }
    assert app.callback_submissions("op-ci-terminal-1") == ()
    assert app.async_callback_rejections("op-ci-terminal-1") == (
        {
            "operationId": "op-ci-terminal-1",
            "callbackId": "cb-terminal",
            "idempotencyKey": "idem-callback-terminal",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-terminal-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "status": "cancelled",
            "reason": "terminal_run",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )
    assert app.late_async_callbacks("op-ci-terminal-1") == (
        {
            "kind": "LateExternalCallbackReceived",
            "operationId": "op-ci-terminal-1",
            "callbackId": "cb-terminal",
            "idempotencyKey": "idem-callback-terminal",
            **_callback_rejection_metadata({"status": "completed"}),
            "runId": "run-terminal-1",
            "nodeId": "waitCI",
            "attemptId": "attempt-1",
            "status": "cancelled",
            "reason": "terminal_run",
            "receivedAt": "2026-07-03T00:00:02Z",
        },
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-terminal-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-terminal-1:2"},
            cookies={},
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-terminal-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    event_payload = json.loads(events.body.decode("utf-8"))
    status_payload = json.loads(status.body.decode("utf-8"))
    assert events.status_code == 200
    assert event_payload["lastCursor"] == "run-terminal-1:3"
    assert [event["kind"] for event in event_payload["events"]] == ["LateExternalCallbackReceived"]
    late_event = event_payload["events"][0]
    assert late_event["metadata"]["sequence"] == 3
    assert late_event["metadata"]["cursor"] == "run-terminal-1:3"
    assert late_event["metadata"]["operationId"] == "op-ci-terminal-1"
    assert late_event["metadata"]["nodeId"] == "waitCI"
    assert late_event["metadata"]["visibility"] == "operator"
    assert late_event["payload"] == {
        "callbackId": "cb-terminal",
        "idempotencyKey": "idem-callback-terminal",
        "payloadDigest": graphblocks.canonical_hash({"status": "completed"}),
        "verifiedBy": "callback-relay",
        "policySnapshotId": "local",
        "attemptId": "attempt-1",
        "status": "cancelled",
        "reason": "terminal_run",
        "receivedAt": "2026-07-03T00:00:02Z",
    }
    assert status_payload["state"] == "cancelled"
    assert status_payload["completedAt"] == "2026-07-03T00:00:01Z"
    assert status_payload["updatedAt"] == "2026-07-03T00:00:02Z"
    assert status_payload["waitingOn"] == []
    assert status_payload["activeOperations"] == []


def test_server_app_deduplicates_async_callback_sequence_deterministically() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    sequence = [
        ("idem-callback-1", "cb-1", "completed"),
        ("idem-callback-2", "cb-2", "completed"),
        ("idem-callback-1", "cb-1", "completed"),
        ("idem-callback-3", "cb-3", "failed"),
        ("idem-callback-2", "cb-2", "completed"),
        ("idem-callback-3", "cb-3", "failed"),
    ]

    statuses = []
    for index, (idempotency_key, callback_id, status) in enumerate(sequence):
        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": idempotency_key},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": callback_id,
                        "attempt_id": "attempt-1",
                        "payload": {"status": status},
                    }
                ).encode("utf-8"),
                requested_at=f"2026-07-02T00:00:0{index}Z",
            )
        )
        statuses.append(response.status_code)

    assert statuses == [202, 409, 200, 409, 409, 409]
    assert [submission.idempotency_key for submission in app.callback_submissions("op-ci-1")] == [
        "idem-callback-1",
    ]
    assert [
        rejection["reason"] for rejection in app.async_callback_rejections("op-ci-1")
    ] == [
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
    ]


def test_server_async_callback_submission_deep_freezes_nested_payload() -> None:
    payload = {"checks": [{"name": "unit", "status": "passed"}], "summary": {"passed": True}}
    submission = ServerAsyncCallbackSubmission(
        operation_id="op-ci-1",
        callback_id="cb-1",
        idempotency_key="idem-callback-1",
        payload=payload,
    )

    payload["checks"][0]["status"] = "failed"  # type: ignore[index]
    payload["summary"]["passed"] = False  # type: ignore[index]

    assert submission.payload == {"checks": ({"name": "unit", "status": "passed"},), "summary": {"passed": True}}
    assert submission.payload_digest == graphblocks.canonical_hash(
        {"checks": [{"name": "unit", "status": "passed"}], "summary": {"passed": True}}
    )
    with pytest.raises(TypeError):
        submission.payload["summary"]["passed"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        submission.payload["checks"][0]["status"] = "failed"  # type: ignore[index]


def test_server_async_callback_submission_preserves_artifacts() -> None:
    artifacts = [{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}]
    submission = ServerAsyncCallbackSubmission(
        operation_id="op-ci-1",
        callback_id="cb-1",
        idempotency_key="idem-callback-1",
        payload={"status": "completed"},
        artifacts=artifacts,
    )

    artifacts[0]["uri"] = "blob://ci/mutated"

    assert submission.artifacts == ({"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"},)
    with pytest.raises(TypeError):
        submission.artifacts[0]["uri"] = "blob://ci/caller-mutation"
    assert submission.response_payload()["artifacts"] == [
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}
    ]


def test_server_async_callback_response_projects_scope_fences() -> None:
    submission = ServerAsyncCallbackSubmission(
        operation_id="op-ci-1",
        callback_id="cb-1",
        idempotency_key="idem-callback-1",
        payload={"status": "completed"},
        run_id="run-1",
        node_id="waitCI",
        attempt_id="attempt-1",
        provider_operation_id="provider-ci-1",
    )

    payload = submission.response_payload()

    assert payload["runId"] == "run-1"
    assert payload["nodeId"] == "waitCI"
    assert payload["attemptId"] == "attempt-1"
    assert payload["providerOperationId"] == "provider-ci-1"


def test_server_async_callback_from_request_preserves_artifacts() -> None:
    request = ServerRequest(
        method="POST",
        path="/callbacks/op-ci-1",
        headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
        query={},
        cookies={},
        body=json.dumps(
            {
                "callback_id": "cb-1",
                "payload": {"status": "completed"},
                "artifacts": [{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}],
            }
        ).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    submission = ServerAsyncCallbackSubmission.from_request(
        operation_id="op-ci-1",
        request=request,
        verified_by="callback-relay",
    )

    assert submission.artifacts == ({"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"},)
    assert submission.response_payload()["artifacts"] == [
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log"}
    ]


def test_server_async_callback_from_request_accepts_camel_case_artifacts() -> None:
    request = ServerRequest(
        method="POST",
        path="/callbacks/op-ci-1",
        headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
        query={},
        cookies={},
        body=json.dumps(
            {
                "callback_id": "cb-1",
                "payload": {"status": "completed"},
                "artifacts": [
                    {
                        "artifactId": "artifact-ci-log",
                        "uri": "blob://ci/log",
                        "mediaType": "application/json",
                        "sizeBytes": 128,
                    }
                ],
            }
        ).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    submission = ServerAsyncCallbackSubmission.from_request(
        operation_id="op-ci-1",
        request=request,
        verified_by="callback-relay",
    )

    assert submission.artifacts == (
        {
            "artifact_id": "artifact-ci-log",
            "uri": "blob://ci/log",
            "media_type": "application/json",
            "size_bytes": 128,
        },
    )
    assert submission.response_payload()["artifacts"] == [
        {
            "artifact_id": "artifact-ci-log",
            "uri": "blob://ci/log",
            "media_type": "application/json",
            "size_bytes": 128,
        }
    ]


def test_server_async_callback_from_request_validates_payload_digest() -> None:
    payload = {"status": "completed"}
    request = ServerRequest(
        method="POST",
        path="/callbacks/op-ci-1",
        headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
        query={},
        cookies={},
        body=json.dumps(
            {
                "callback_id": "cb-1",
                "payload": payload,
                "payloadDigest": graphblocks.canonical_hash(payload),
            }
        ).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    submission = ServerAsyncCallbackSubmission.from_request(
        operation_id="op-ci-1",
        request=request,
        verified_by="callback-relay",
    )

    assert submission.payload_digest == graphblocks.canonical_hash(payload)

    with pytest.raises(ValueError, match="server async callback payload_digest must match payload"):
        ServerAsyncCallbackSubmission.from_request(
            operation_id="op-ci-1",
            request=ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "payload": payload,
                        "payloadDigest": graphblocks.canonical_hash({"status": "failed"}),
                    }
                ).encode("utf-8"),
                requested_at="2026-07-02T00:00:00Z",
            ),
            verified_by="callback-relay",
        )


def test_server_async_callback_from_request_rejects_conflicting_payload_digest_aliases() -> None:
    payload = {"status": "completed"}
    with pytest.raises(ValueError, match="server async callback payload_digest aliases must not conflict"):
        ServerAsyncCallbackSubmission.from_request(
            operation_id="op-ci-1",
            request=ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "payload": payload,
                        "payload_digest": graphblocks.canonical_hash(payload),
                        "payloadDigest": graphblocks.canonical_hash({"status": "failed"}),
                    }
                ).encode("utf-8"),
                requested_at="2026-07-02T00:00:00Z",
            ),
            verified_by="callback-relay",
        )


def test_server_async_callback_from_request_rejects_conflicting_callback_id_aliases() -> None:
    with pytest.raises(ValueError, match="server async callback callback_id aliases must not conflict"):
        ServerAsyncCallbackSubmission.from_request(
            operation_id="op-ci-1",
            request=ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "callbackId": "cb-2",
                        "payload": {"status": "completed"},
                    }
                ).encode("utf-8"),
                requested_at="2026-07-02T00:00:00Z",
            ),
            verified_by="callback-relay",
        )


def test_server_async_callback_from_request_rejects_body_header_idempotency_conflict() -> None:
    with pytest.raises(ValueError, match="server async callback idempotency_key body/header values must not conflict"):
        ServerAsyncCallbackSubmission.from_request(
            operation_id="op-ci-1",
            request=ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"GraphBlocks-Idempotency-Key": "idem-header"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "idempotencyKey": "idem-body",
                        "payload": {"status": "completed"},
                    }
                ).encode("utf-8"),
                requested_at="2026-07-02T00:00:00Z",
            ),
            verified_by="callback-relay",
        )


def test_server_async_callback_from_request_rejects_conflicting_idempotency_headers() -> None:
    with pytest.raises(ValueError, match="server async callback idempotency_key header values must not conflict"):
        ServerAsyncCallbackSubmission.from_request(
            operation_id="op-ci-1",
            request=ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={
                    "GraphBlocks-Idempotency-Key": "idem-graphblocks",
                    "Idempotency-Key": "idem-legacy",
                },
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": "cb-1",
                        "payload": {"status": "completed"},
                    }
                ).encode("utf-8"),
                requested_at="2026-07-02T00:00:00Z",
            ),
            verified_by="callback-relay",
        )


def test_server_async_callback_submission_rejects_invalid_artifacts() -> None:
    with pytest.raises(ValueError, match="server async callback artifacts must be a sequence"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts={"artifact_id": "artifact-ci-log"},  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="server async callback artifacts entries must be JSON objects"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts=["artifact-ci-log"],  # type: ignore[list-item]
        )

    with pytest.raises(ValueError, match="server async callback artifacts uri must be a non-empty string"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts=[{"artifact_id": "artifact-ci-log"}],
        )

    with pytest.raises(ValueError, match="server async callback artifacts media_type must be a non-empty string"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts=[{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "media_type": " "}],
        )

    with pytest.raises(ValueError, match="server async callback artifacts checksum must be a non-empty string"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts=[{"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "checksum": ""}],
        )

    invalid_sizes = (
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "size_bytes": True},
        {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log", "size_bytes": -1},
        {"artifactId": "artifact-ci-log", "uri": "blob://ci/log", "sizeBytes": "128"},
    )
    for artifact in invalid_sizes:
        with pytest.raises(
            ValueError,
            match="server async callback artifacts size_bytes must be a non-negative integer",
        ):
            ServerAsyncCallbackSubmission(
                operation_id="op-ci-1",
                callback_id="cb-1",
                idempotency_key="idem-callback-1",
                payload={"status": "completed"},
                artifacts=[artifact],
            )

    with pytest.raises(ValueError, match="server async callback artifacts must not contain duplicate artifact_id"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            artifacts=[
                {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log-1"},
                {"artifact_id": "artifact-ci-log", "uri": "blob://ci/log-2"},
            ],
        )


def test_server_async_callback_submission_rejects_payload_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="server async callback payload_digest must match payload"):
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            payload_digest="sha256:not-the-payload",
        )


def test_server_app_callback_idempotency_survives_returned_payload_mutation_attempts() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    body = {
        "callback_id": "cb-1",
        "attempt_id": "attempt-1",
        "payload": {"checks": [{"name": "unit", "status": "passed"}], "summary": {"passed": True}},
    }
    request = ServerRequest(
        method="POST",
        path="/callbacks/op-ci-1",
        headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-1"},
        query={},
        cookies={},
        body=json.dumps(body).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    first = app.handle(request)
    stored = app.callback_submissions("op-ci-1")[0]

    assert first.status_code == 202
    with pytest.raises(TypeError):
        stored.payload["checks"][0]["status"] = "failed"  # type: ignore[index]

    duplicate = app.handle(request)

    assert duplicate.status_code == 200
    assert len(app.callback_submissions("op-ci-1")) == 1


def test_server_app_deduplicates_nested_callback_payload_sequence_deterministically() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))
    sequence = [
        ("idem-callback-1", "cb-1", {"checks": [{"name": "lint", "status": "passed"}]}),
        ("idem-callback-2", "cb-2", {"checks": [{"name": "unit", "status": "passed"}]}),
        ("idem-callback-1", "cb-1", {"checks": [{"name": "lint", "status": "passed"}]}),
        ("idem-callback-3", "cb-3", {"checks": [{"name": "typecheck", "status": "failed"}]}),
        ("idem-callback-2", "cb-2", {"checks": [{"name": "unit", "status": "passed"}]}),
        ("idem-callback-3", "cb-3", {"checks": [{"name": "typecheck", "status": "failed"}]}),
    ]

    statuses = []
    for index, (idempotency_key, callback_id, payload) in enumerate(sequence):
        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/op-ci-1",
                headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": idempotency_key},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "callback_id": callback_id,
                        "attempt_id": "attempt-1",
                        "payload": payload,
                    }
                ).encode("utf-8"),
                requested_at=f"2026-07-02T00:00:0{index}Z",
            )
        )
        statuses.append(response.status_code)

    assert statuses == [202, 409, 200, 409, 409, 409]
    assert [submission.idempotency_key for submission in app.callback_submissions("op-ci-1")] == [
        "idem-callback-1",
    ]
    assert [
        rejection["reason"] for rejection in app.async_callback_rejections("op-ci-1")
    ] == [
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
        "duplicate_operation_receipt",
    ]


def test_server_app_rejects_malformed_async_callback_submission() -> None:
    app = GraphBlocksServerApp()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-1",
            headers={"GraphBlocks-Idempotency-Key": "idem-callback-1"},
            query={},
            cookies={},
            body=json.dumps({"callback_id": "cb-1", "payload": ["not", "object"]}).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server async callback payload must be a JSON object",
    }


def test_server_app_rejects_async_callback_with_invalid_received_timestamp() -> None:
    app = GraphBlocksServerApp()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-invalid-time",
            headers={"GraphBlocks-Idempotency-Key": "idem-callback-invalid-time"},
            query={},
            cookies={},
            body=json.dumps({"callback_id": "cb-invalid-time", "payload": {"status": "completed"}}).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server async callback received_at must be an ISO datetime",
    }
    assert app.callback_submissions("op-ci-invalid-time") == ()

    space_separator = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-invalid-time",
            headers={"GraphBlocks-Idempotency-Key": "idem-callback-space-time"},
            query={},
            cookies={},
            body=json.dumps({"callback_id": "cb-space-time", "payload": {"status": "completed"}}).encode("utf-8"),
            requested_at="2026-07-02 00:00:00Z",
        )
    )

    assert space_separator.status_code == 400
    assert json.loads(space_separator.body.decode("utf-8")) == {
        "ok": False,
        "error": "server async callback received_at must be an ISO datetime",
    }
    assert app.callback_submissions("op-ci-invalid-time") == ()


def test_server_app_rejects_async_callback_with_invalid_policy_snapshot_id() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-invalid-policy",
            headers={"Authorization": "Bearer token-1", "GraphBlocks-Idempotency-Key": "idem-callback-invalid-policy"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callback_id": "cb-invalid-policy",
                    "attempt_id": "attempt-1",
                    "policySnapshotId": " ",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server async callback policy_snapshot_id must not be empty",
    }
    assert app.callback_submissions("op-ci-invalid-policy") == ()


def test_server_app_rejects_oversized_async_callback_payload_before_storage() -> None:
    app = GraphBlocksServerApp(max_async_callback_payload_bytes=32)

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-large",
            headers={"GraphBlocks-Idempotency-Key": "idem-callback-large"},
            query={},
            cookies={},
            body=json.dumps({"callback_id": "cb-large", "payload": {"log": "x" * 64}}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 413
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "operationId": "op-ci-large",
        "payloadSizeBytes": 74,
        "maxPayloadBytes": 32,
        "error": "async callback payload exceeds max payload bytes",
    }
    assert app.callback_submissions("op-ci-large") == ()
    assert app.async_callback_rejections("op-ci-large") == (
        {
            "operationId": "op-ci-large",
            "callbackId": "cb-large",
            "idempotencyKey": "idem-callback-large",
            **_callback_rejection_metadata({"log": "x" * 64}, verified_by="unauthenticated"),
            "reason": "payload_too_large",
            "receivedAt": "2026-07-03T00:00:00Z",
        },
    )


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
    assert payload["events"][1]["payload"]["outputs"] == {"prompt": "Events ok"}
    with pytest.raises(TypeError):
        app._events_by_run_id["run-events-1"][0]["metadata"]["responseId"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        app._events_by_run_id["run-events-1"][1]["payload"]["outputs"]["prompt"] = "changed"  # type: ignore[index]


def test_server_app_replays_stored_run_events_after_cursor_query() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-events-cursor"},
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
                    "inputs": {"message": {"text": "cursor"}},
                    "runId": "run-events-cursor-1",
                    "responseId": "response-events-cursor-1",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-cursor-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-events-cursor-1:1"},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload["runId"] == "run-events-cursor-1"
    assert payload["replayFromCursor"] == "run-events-cursor-1:1"
    assert payload["lastCursor"] == "run-events-cursor-1:2"
    assert [event["kind"] for event in payload["events"]] == ["RunSucceeded"]
    assert payload["events"][0]["payload"]["outputs"] == {"prompt": "Events cursor"}


def test_server_app_run_event_replay_filters_visibility_by_principal() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "client-token": PrincipalRef("user-1"),
                "operator-token": PrincipalRef("operator-1", roles=("operator",)),
            }
        )
    )
    app._events_by_run_id["run-events-visibility-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-events-visibility-1"},
            "metadata": {
                "eventId": "evt-client",
                "runId": "run-events-visibility-1",
                "sequence": 1,
                "cursor": "run-events-visibility-1:1",
                "releaseId": "release-events-visibility-1",
                "occurredAt": "2026-07-03T00:00:00Z",
                "visibility": "client",
            },
        },
        {
            "kind": "ExternalCallbackReceived",
            "payload": {"callbackId": "cb-visibility"},
            "metadata": {
                "eventId": "evt-operator",
                "runId": "run-events-visibility-1",
                "sequence": 2,
                "cursor": "run-events-visibility-1:2",
                "releaseId": "release-events-visibility-1",
                "occurredAt": "2026-07-03T00:00:01Z",
                "visibility": "operator",
                "operationId": "op-ci-visibility-1",
            },
        },
    )

    client_response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-visibility-1/events",
            headers={"Authorization": "Bearer client-token"},
            query={"cursor": "run-events-visibility-1:0"},
            cookies={},
        )
    )
    operator_response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-visibility-1/events",
            headers={"Authorization": "Bearer operator-token"},
            query={"cursor": "run-events-visibility-1:0"},
            cookies={},
        )
    )

    client_payload = json.loads(client_response.body.decode("utf-8"))
    operator_payload = json.loads(operator_response.body.decode("utf-8"))
    assert client_response.status_code == 200
    assert operator_response.status_code == 200
    assert client_payload["lastCursor"] == "run-events-visibility-1:2"
    assert [event["metadata"]["eventId"] for event in client_payload["events"]] == ["evt-client"]
    assert operator_payload["lastCursor"] == "run-events-visibility-1:2"
    assert [event["metadata"]["eventId"] for event in operator_payload["events"]] == [
        "evt-client",
        "evt-operator",
    ]


def test_server_app_rejects_malformed_stored_event_cursor_query() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-events-cursor-format-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-cursor-format-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-events-cursor-format-1:not-a-sequence"},
            cookies={},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "application events cursor must use '<run_id>:<sequence>' with a non-negative integer sequence",
    }


def test_server_app_rejects_stored_event_replay_with_malformed_sequence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-events-bool-sequence-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": True},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-bool-sequence-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "application events sequence must be an integer",
    }


def test_server_app_reports_stored_event_cursor_expired() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-events-cursor-expired-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {
                "eventId": "evt-start",
                "sequence": 1,
                "releaseId": "release-events-cursor-expired-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-events-cursor-expired-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={"cursor": "run-events-cursor-expired-1:99"},
            cookies={},
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "CursorExpired",
        "runId": "run-events-cursor-expired-1",
        "requestedCursor": "run-events-cursor-expired-1:99",
        "nearestAvailableCursor": "run-events-cursor-expired-1:1",
        "lastCursor": "run-events-cursor-expired-1:1",
        "lastSequence": 1,
        "runStatus": {
            "runId": "run-events-cursor-expired-1",
            "state": "running",
            "releaseId": "release-events-cursor-expired-1",
            "lastCursor": "run-events-cursor-expired-1:1",
            "startedAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:00Z",
            "completedAt": None,
            "waitingOn": [],
            "activeOperations": [],
        },
    }


def test_server_app_reports_run_status_from_authoritative_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-status"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Status {message.text}"},
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
                    "runId": "run-status-1",
                    "responseId": "response-status-1",
                    "releaseId": "release-status-1",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-status-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload == {
        "ok": True,
        "runId": "run-status-1",
        "state": "succeeded",
        "releaseId": "release-status-1",
        "lastCursor": "run-status-1:2",
        "startedAt": "2026-07-02T00:00:00Z",
        "updatedAt": "2026-07-02T00:00:00Z",
        "completedAt": "2026-07-02T00:00:00Z",
        "waitingOn": [],
        "activeOperations": [],
    }


def test_server_app_rejects_run_status_without_event_timestamps() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-status-missing-time-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-status-missing-time-1"},
            "metadata": {
                "runId": "run-status-missing-time-1",
                "sequence": 1,
                "cursor": "run-status-missing-time-1:1",
                "releaseId": "release-status-missing-time-1",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-status-missing-time-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server run status occurredAt must be an ISO datetime",
    }


def test_server_app_rejects_run_status_with_malformed_event_sequence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-status-bool-sequence-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-status-bool-sequence-1"},
            "metadata": {
                "runId": "run-status-bool-sequence-1",
                "sequence": True,
                "cursor": "run-status-bool-sequence-1:1",
                "releaseId": "release-status-bool-sequence-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-status-bool-sequence-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server run status sequence must be an integer",
    }


def test_server_app_terminal_run_status_overrides_stale_control_projection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-status-terminal-control-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-status-terminal-control-1"},
            "metadata": {
                "runId": "run-status-terminal-control-1",
                "sequence": 1,
                "cursor": "run-status-terminal-control-1:1",
                "releaseId": "release-status-terminal-control-1",
                "occurredAt": "2026-07-02T00:00:00Z",
            },
        },
        {
            "kind": "RunCompleted",
            "payload": {"status": "completed", "outputs": {}},
            "metadata": {
                "runId": "run-status-terminal-control-1",
                "sequence": 2,
                "cursor": "run-status-terminal-control-1:2",
                "releaseId": "release-status-terminal-control-1",
                "occurredAt": "2026-07-02T00:00:02Z",
            },
        },
    )
    app._run_controls_by_run_id["run-status-terminal-control-1"] = (
        {
            "operation": "pause_run",
            "status": "paused_operator",
            "reason": "operator_hold",
            "occurredAt": "2026-07-02T00:00:01Z",
            "lastCursor": "run-status-terminal-control-1:1",
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-status-terminal-control-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload["state"] == "completed"
    assert payload["completedAt"] == "2026-07-02T00:00:02Z"
    assert payload["waitingOn"] == []


def test_server_app_lists_run_statuses_from_authoritative_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-list-runs"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "List {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    for index, run_id in enumerate(("run-list-2", "run-list-1"), start=1):
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
                        "inputs": {"message": {"text": run_id}},
                        "runId": run_id,
                        "responseId": f"response-list-{index}",
                        "releaseId": "release-list-1",
                        "occurredAt": f"2026-07-02T00:00:0{index}Z",
                    }
                ).encode("utf-8"),
            )
        )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload == {
        "ok": True,
        "runs": [
            {
                "runId": "run-list-1",
                "state": "succeeded",
                "releaseId": "release-list-1",
                "lastCursor": "run-list-1:2",
                "startedAt": "2026-07-02T00:00:02Z",
                "updatedAt": "2026-07-02T00:00:02Z",
                "completedAt": "2026-07-02T00:00:02Z",
                "waitingOn": [],
                "activeOperations": [],
            },
            {
                "runId": "run-list-2",
                "state": "succeeded",
                "releaseId": "release-list-1",
                "lastCursor": "run-list-2:2",
                "startedAt": "2026-07-02T00:00:01Z",
                "updatedAt": "2026-07-02T00:00:01Z",
                "completedAt": "2026-07-02T00:00:01Z",
                "waitingOn": [],
                "activeOperations": [],
            },
        ],
    }


def test_server_app_attaches_to_run_after_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-attach"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Attach {message.text}"},
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
                    "runId": "run-attach-1",
                    "responseId": "response-attach-1",
                    "releaseId": "release-attach-1",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-attach-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "last_cursor": "run-attach-1:1",
                    "capabilities": ["assistant_drafts", "retractions"],
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["runId"] == "run-attach-1"
    assert payload["lastCursor"] == "run-attach-1:2"
    assert payload["liveCursor"] == "run-attach-1:2"
    assert payload["replayComplete"] is True
    assert payload["capabilities"] == ["assistant_drafts", "retractions"]
    assert [event["kind"] for event in payload["events"]] == ["RunSucceeded"]


def test_server_app_rejects_attach_with_invalid_capabilities() -> None:
    cases = (
        (["assistant_drafts "], "attach request capabilities must contain only supported attach capability literals"),
        (["screen_share"], "attach request capabilities must contain only supported attach capability literals"),
    )
    for index, (capabilities, expected_error) in enumerate(cases, start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id[f"run-attach-invalid-capability-{index}"] = ()

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/runs/run-attach-invalid-capability-{index}/attach",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps({"capabilities": capabilities}).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }


def test_server_app_rejects_attach_replay_with_malformed_sequence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-attach-bool-sequence-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": True},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-attach-bool-sequence-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({}).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "attach request sequence must be an integer",
    }


def test_server_app_reports_attach_cursor_expired_for_unknown_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-attach-expired"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Attach {message.text}"},
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
                    "runId": "run-attach-expired-1",
                    "responseId": "response-attach-expired-1",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-attach-expired-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": "run-attach-expired-1:99"}).encode("utf-8"),
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "CursorExpired",
        "runId": "run-attach-expired-1",
        "requestedCursor": "run-attach-expired-1:99",
        "nearestAvailableCursor": "run-attach-expired-1:1",
        "lastCursor": "run-attach-expired-1:2",
        "lastSequence": 2,
        "runStatus": {
            "runId": "run-attach-expired-1",
            "state": "succeeded",
            "releaseId": "local",
            "lastCursor": "run-attach-expired-1:2",
            "startedAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:00Z",
            "completedAt": "2026-07-02T00:00:00Z",
            "waitingOn": [],
            "activeOperations": [],
        },
    }


def test_server_app_rejects_attach_cursor_for_different_run() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-attach-cursor-scope-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-attach-cursor-scope-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": "other-run:1"}).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "attach request last_cursor must belong to run 'run-attach-cursor-scope-1'",
    }


def test_server_app_rejects_malformed_attach_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-attach-cursor-format-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-attach-cursor-format-1/attach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": "run-attach-cursor-format-1:not-a-sequence"}).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "attach request last_cursor must use '<run_id>:<sequence>' with a non-negative integer sequence",
    }


def test_server_app_detaches_from_run_without_cancelling_or_dropping_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-detach"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Detach {message.text}"},
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
                    "runId": "run-detach-1",
                    "responseId": "response-detach-1",
                }
            ).encode("utf-8"),
        )
    )
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"client_id": "client-1", "reason": "tab_closed"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-detach-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-detach-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-detach-1",
        "clientId": "client-1",
        "reason": "tab_closed",
        "status": "detached",
        "lastCursor": "run-detach-1:2",
    }
    assert app.detachments("run-detach-1") == (
        {
            "clientId": "client-1",
            "reason": "tab_closed",
            "detachedAt": "2026-07-02T00:00:00Z",
            "lastCursor": "run-detach-1:2",
        },
    )
    with pytest.raises(TypeError):
        app.detachments("run-detach-1")[0]["reason"] = "changed"
    assert events.status_code == 200
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]
    assert json.loads(status.body.decode("utf-8"))["state"] == "succeeded"


def test_server_app_treats_repeated_detach_from_same_client_as_idempotent() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-detach-idempotent"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Detach {message.text}"},
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
                    "runId": "run-detach-idempotent-1",
                    "responseId": "response-detach-idempotent-1",
                }
            ).encode("utf-8"),
        )
    )
    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-idempotent-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"client_id": "client-1", "reason": "tab_closed"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-idempotent-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"client_id": "client-1", "reason": "network_retry"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:05Z",
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-detach-idempotent-1",
        "clientId": "client-1",
        "reason": "tab_closed",
        "status": "detached",
        "lastCursor": "run-detach-idempotent-1:2",
        "detachedAt": "2026-07-03T00:00:00Z",
        "duplicate": True,
    }
    assert app.detachments("run-detach-idempotent-1") == (
        {
            "clientId": "client-1",
            "reason": "tab_closed",
            "detachedAt": "2026-07-03T00:00:00Z",
            "lastCursor": "run-detach-idempotent-1:2",
        },
    )


def test_server_app_rejects_detach_for_missing_run_or_client_id() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-detach-invalid"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Detach {message.text}"},
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
                    "runId": "run-detach-invalid-1",
                    "responseId": "response-detach-invalid-1",
                }
            ).encode("utf-8"),
        )
    )

    missing = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/missing-run/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"clientId": "client-1"}).encode("utf-8"),
        )
    )
    malformed = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-invalid-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "no-client"}).encode("utf-8"),
        )
    )

    assert missing.status_code == 404
    assert json.loads(missing.body.decode("utf-8")) == {
        "ok": False,
        "error": "run detach stream not found for run 'missing-run'",
    }
    assert malformed.status_code == 400
    assert json.loads(malformed.body.decode("utf-8")) == {
        "ok": False,
        "error": "detach request client_id must not be empty",
    }


def test_server_app_rejects_detach_with_whitespace_wrapped_client_id_or_reason() -> None:
    cases = (
        (
            {"clientId": "client-1 "},
            "detach request client_id must not contain surrounding whitespace",
        ),
        (
            {"clientId": "client-1", "reason": " tab_closed"},
            "detach request reason must not contain surrounding whitespace",
        ),
    )
    for index, (body, expected_error) in enumerate(cases, start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id[f"run-detach-whitespace-{index}"] = (
            {
                "kind": "RunStarted",
                "payload": {"runId": f"run-detach-whitespace-{index}"},
                "metadata": {
                    "runId": f"run-detach-whitespace-{index}",
                    "sequence": 1,
                    "cursor": f"run-detach-whitespace-{index}:1",
                    "eventId": f"evt-detach-whitespace-{index}",
                    "releaseId": f"release-detach-whitespace-{index}",
                    "occurredAt": "2026-07-02T00:00:00Z",
                },
            },
        )

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/runs/run-detach-whitespace-{index}/detach",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(body).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.detachments(f"run-detach-whitespace-{index}") == ()


def test_server_app_rejects_detach_with_invalid_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-detach-invalid-time-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-detach-invalid-time-1"},
            "metadata": {
                "runId": "run-detach-invalid-time-1",
                "sequence": 1,
                "cursor": "run-detach-invalid-time-1:1",
                "releaseId": "release-detach-invalid-time-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-invalid-time-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"clientId": "client-1"}).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "detach request detached_at must be an ISO datetime",
    }
    assert app.detachments("run-detach-invalid-time-1") == ()


def test_server_app_rejects_detach_when_retained_event_sequence_is_malformed() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-detach-bool-sequence-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-detach-bool-sequence-1"},
            "metadata": {
                "runId": "run-detach-bool-sequence-1",
                "sequence": True,
                "cursor": "run-detach-bool-sequence-1:1",
                "releaseId": "release-detach-bool-sequence-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-detach-bool-sequence-1/detach",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"clientId": "client-1"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "detach request sequence must be an integer",
    }
    assert app.detachments("run-detach-bool-sequence-1") == ()


def test_server_app_subscribes_to_run_events_with_filtered_replay() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-subscribe"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Subscribe {message.text}"},
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
                    "runId": "run-subscribe-1",
                    "responseId": "response-subscribe-1",
                    "releaseId": "release-subscribe-1",
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscription_id": "sub-run-1",
                    "event_filter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "local_callback",
                        "callback_name": "ide",
                        "options": {"priority": "normal"},
                    },
                    "replay_from_cursor": "run-subscribe-1:1",
                    "failure_policy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert payload["ok"] is True
    assert payload["subscriptionId"] == "sub-run-1"
    assert payload["runId"] == "run-subscribe-1"
    assert payload["status"] == "active"
    assert payload["failurePolicy"] == "best_effort"
    assert payload["replayFromCursor"] == "run-subscribe-1:1"
    assert payload["lastCursor"] == "run-subscribe-1:2"
    assert payload["owner"] == {
        "principalId": "user-1",
        "tenantId": None,
        "groups": [],
        "roles": [],
        "attributes": {},
    }
    assert payload["eventFilter"] == {"types": ["RunSucceeded"], "visibility": ["client"]}
    assert payload["delivery"] == {
        "kind": "local_callback",
        "callback_name": "ide",
        "options": {"priority": "normal"},
    }
    assert [event["kind"] for event in payload["events"]] == ["RunSucceeded"]
    assert app.subscriptions("run-subscribe-1") == (
        ServerEventSubscription(
            subscription_id="sub-run-1",
            run_id="run-subscribe-1",
            event_filter={"types": ("RunSucceeded",), "visibility": ("client",)},
            delivery={
                "kind": "local_callback",
                "callback_name": "ide",
                "options": {"priority": "normal"},
            },
            status="active",
            failure_policy="best_effort",
            replay_from_cursor="run-subscribe-1:1",
            created_at="2026-07-02T00:00:00Z",
            owner=PrincipalRef("user-1"),
        ),
    )
    with pytest.raises(TypeError):
        app.subscriptions("run-subscribe-1")[0].delivery["options"]["priority"] = "high"  # type: ignore[index]


def test_server_app_rejects_duplicate_subscription_id_without_overwrite() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-subscribe-duplicate"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Duplicate {message.text}"},
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
                    "runId": "run-subscribe-duplicate-1",
                    "responseId": "response-subscribe-duplicate-1",
                }
            ).encode("utf-8"),
        )
    )

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-duplicate-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-duplicate-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-duplicate-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-duplicate-1",
                    "eventFilter": {"types": ["RunFailed"]},
                    "delivery": {"kind": "local_callback", "callback_name": "other-ide"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:05Z",
        )
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-subscribe-duplicate-1",
        "subscriptionId": "sub-duplicate-1",
        "state": "active",
        "error": "subscription 'sub-duplicate-1' already exists for run 'run-subscribe-duplicate-1'",
    }
    assert app.subscriptions("run-subscribe-duplicate-1") == (
        ServerEventSubscription(
            subscription_id="sub-duplicate-1",
            run_id="run-subscribe-duplicate-1",
            event_filter={"types": ("RunSucceeded",), "visibility": ("client",)},
            delivery={"kind": "local_callback", "callback_name": "ide"},
            status="active",
            failure_policy="retry_then_dead_letter",
            created_at="2026-07-03T00:00:00Z",
            owner=PrincipalRef("user-1"),
        ),
    )


def test_server_app_rejects_impossible_ordered_event_subscription_delivery() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-ordering-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-subscribe-ordering-1"},
            "metadata": {
                "runId": "run-subscribe-ordering-1",
                "sequence": 1,
                "cursor": "run-subscribe-ordering-1:1",
                "releaseId": "release-subscribe-ordering-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-ordering-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ordering-1",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {
                        "kind": "local_callback",
                        "callback_name": "ide",
                        "ordering": {"scope": "run", "mode": "ordered"},
                    },
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription delivery.ordering requests ordered delivery on an unsupported target",
    }
    assert app.subscriptions("run-subscribe-ordering-1") == ()


def test_server_app_subscribes_from_accepted_run_initial_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-subscribe-initial-cursor"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Initial {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    accepted = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "cursor"}},
                    "runId": "run-subscribe-initial-1",
                    "responseId": "response-subscribe-initial-1",
                    "responseMode": "accepted",
                }
            ).encode("utf-8"),
        )
    )
    initial_cursor = json.loads(accepted.body.decode("utf-8"))["initialCursor"]

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-initial-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-initial",
                    "eventFilter": {"types": ["RunStarted", "RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "replayFromCursor": initial_cursor,
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert payload["replayFromCursor"] == "run-subscribe-initial-1:0"
    assert payload["lastCursor"] == "run-subscribe-initial-1:2"
    assert [event["kind"] for event in payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert payload["events"][1]["payload"]["outputs"] == {"prompt": "Initial cursor"}


def test_server_app_subscription_replay_filters_visibility_node_operation_and_severity() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1", roles=("operator",))})
    )
    app._events_by_run_id["run-subscribe-filter-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-warning", "sequence": 1},
            "payload": {
                "visibility": "operator",
                "node_id": "runChecks",
                "operation_id": "op-ci-1",
                "severity": "warning",
            },
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-client", "sequence": 2},
            "payload": {
                "visibility": "client",
                "node_id": "runChecks",
                "operation_id": "op-ci-1",
                "severity": "error",
            },
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-wrong-node", "sequence": 3},
            "payload": {
                "visibility": "operator",
                "node_id": "review",
                "operation_id": "op-ci-1",
                "severity": "critical",
            },
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-matching", "sequence": 4},
            "payload": {
                "visibility": "operator",
                "node_id": "runChecks",
                "operation_id": "op-ci-1",
                "severity": "error",
            },
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-filter-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-filter-1",
                    "eventFilter": {
                        "types": ["JobProgress"],
                        "visibility": ["operator"],
                        "nodeIds": ["runChecks"],
                        "operationIds": ["op-ci-1"],
                        "severityMin": "error",
                    },
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 201
    assert [event["metadata"]["eventId"] for event in payload["events"]] == ["event-matching"]


def test_server_app_subscription_replay_filters_top_level_node_and_operation_fields() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1", roles=("operator",))})
    )
    app._events_by_run_id["run-subscribe-top-level-filter-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-wrong-operation", "sequence": 1},
            "nodeId": "runChecks",
            "operationId": "op-ci-other",
            "payload": {"visibility": "operator", "severity": "error"},
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-matching", "sequence": 2},
            "nodeId": "runChecks",
            "operationId": "op-ci-1",
            "payload": {"visibility": "operator", "severity": "error"},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-top-level-filter-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-filter-top-level-1",
                    "eventFilter": {
                        "types": ["JobProgress"],
                        "visibility": ["operator"],
                        "nodeIds": ["runChecks"],
                        "operationIds": ["op-ci-1"],
                    },
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert [event["metadata"]["eventId"] for event in payload["events"]] == ["event-matching"]


def test_server_app_subscription_replay_filters_top_level_visibility_field() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1", roles=("operator",))})
    )
    app._events_by_run_id["run-subscribe-top-level-visibility-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-internal", "sequence": 1},
            "visibility": "internal",
            "payload": {"severity": "error"},
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-operator", "sequence": 2},
            "visibility": "operator",
            "payload": {"severity": "error"},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-top-level-visibility-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-filter-top-level-visibility-1",
                    "eventFilter": {"types": ["JobProgress"], "visibility": ["operator"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert [event["metadata"]["eventId"] for event in payload["events"]] == ["event-operator"]


def test_server_app_subscription_replay_rejects_malformed_visibility_as_hidden() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-malformed-visibility-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-invalid-visibility", "sequence": 1, "visibility": True},
            "payload": {"severity": "info"},
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-client", "sequence": 2, "visibility": "client"},
            "payload": {"severity": "info"},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-malformed-visibility-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-malformed-visibility-1",
                    "eventFilter": {"types": ["JobProgress"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert [event["metadata"]["eventId"] for event in payload["events"]] == ["event-client"]


def test_server_app_subscription_replay_includes_terminal_events_by_default() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-terminal-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-progress", "sequence": 1},
            "payload": {"visibility": "client"},
        },
        {
            "kind": "RunSucceeded",
            "metadata": {"eventId": "event-terminal", "sequence": 2},
            "payload": {"visibility": "client", "status": "succeeded"},
        },
    )

    default_response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-terminal-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-terminal-default",
                    "eventFilter": {"types": ["JobProgress"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )
    opt_out_response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-terminal-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-terminal-opt-out",
                    "eventFilter": {"types": ["JobProgress"], "includeTerminalEvents": False},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    default_payload = json.loads(default_response.body.decode("utf-8"))
    opt_out_payload = json.loads(opt_out_response.body.decode("utf-8"))

    assert default_response.status_code == 201
    assert [event["metadata"]["eventId"] for event in default_payload["events"]] == [
        "event-progress",
        "event-terminal",
    ]
    assert opt_out_response.status_code == 201
    assert [event["metadata"]["eventId"] for event in opt_out_payload["events"]] == [
        "event-progress"
    ]


def test_server_app_subscription_replay_excludes_terminal_events_from_broad_filter_when_disabled() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-terminal-broad-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-progress", "sequence": 1},
            "payload": {"visibility": "client"},
        },
        {
            "kind": "RunSucceeded",
            "metadata": {"eventId": "event-terminal", "sequence": 2},
            "payload": {"visibility": "client", "status": "succeeded"},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-terminal-broad-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-terminal-broad-opt-out",
                    "eventFilter": {"includeTerminalEvents": False},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert [event["metadata"]["eventId"] for event in payload["events"]] == ["event-progress"]


def test_server_app_rejects_subscription_replay_with_malformed_sequence() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-bool-sequence-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-progress", "sequence": True},
            "payload": {"visibility": "client"},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-bool-sequence-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-bool-sequence",
                    "eventFilter": {"types": ["JobProgress"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription sequence must be an integer",
    }


def test_server_app_subscribe_events_reports_cursor_expired() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-subscribe-expired"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Subscribe {message.text}"},
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
                    "runId": "run-subscribe-expired-1",
                    "responseId": "response-subscribe-expired-1",
                    "occurredAt": "2026-07-02T00:00:00Z",
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-expired-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-expired",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "replayFromCursor": "run-subscribe-expired-1:99",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "CursorExpired",
        "runId": "run-subscribe-expired-1",
        "requestedCursor": "run-subscribe-expired-1:99",
        "nearestAvailableCursor": "run-subscribe-expired-1:1",
        "lastCursor": "run-subscribe-expired-1:2",
        "lastSequence": 2,
        "runStatus": {
            "runId": "run-subscribe-expired-1",
            "state": "succeeded",
            "releaseId": "local",
            "lastCursor": "run-subscribe-expired-1:2",
            "startedAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:00Z",
            "completedAt": "2026-07-02T00:00:00Z",
            "waitingOn": [],
            "activeOperations": [],
        },
    }
    assert app.subscriptions("run-subscribe-expired-1") == ()


def test_server_app_rejects_subscription_replay_cursor_for_different_run() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-cursor-scope-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-cursor-scope-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-cursor-scope",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "replayFromCursor": "other-run:1",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription replay_from_cursor must belong to run 'run-subscribe-cursor-scope-1'",
    }
    assert app.subscriptions("run-subscribe-cursor-scope-1") == ()


def test_server_app_rejects_malformed_subscription_replay_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-cursor-format-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-cursor-format-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-cursor-format",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "replayFromCursor": "run-subscribe-cursor-format-1:not-a-sequence",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": (
            "server event subscription replay_from_cursor must use '<run_id>:<sequence>' "
            "with a non-negative integer sequence"
        ),
    }
    assert app.subscriptions("run-subscribe-cursor-format-1") == ()


def test_server_app_rejects_subscription_without_delivery_kind() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-subscribe-invalid"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Subscribe {message.text}"},
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
                    "runId": "run-subscribe-invalid-1",
                    "responseId": "response-subscribe-invalid-1",
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-invalid-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {},
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription delivery.kind must not be empty",
    }
    assert app.subscriptions("run-subscribe-invalid-1") == ()


def test_server_app_rejects_subscription_with_invalid_failure_policy() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-policy-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-policy-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-policy-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "retry_forever",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server subscription failure_policy must be one of best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure",
    }
    assert app.subscriptions("run-subscribe-policy-1") == ()


def test_server_app_rejects_subscription_with_invalid_created_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-created-time-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-created-time-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-created-time-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription created_at must be an ISO datetime",
    }
    assert app.subscriptions("run-subscribe-created-time-1") == ()


def test_server_app_rejects_mandatory_subscription_without_retry_or_dead_letter_policy() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-mandatory-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-mandatory-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-mandatory-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "local_callback",
                        "callback_name": "ide",
                        "mandatory": True,
                    },
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription mandatory delivery requires retry, dead-letter, pause-run, or fail-run failure policy",
    }
    assert app.subscriptions("run-subscribe-mandatory-1") == ()


def test_server_app_rejects_mandatory_subscription_failure_policy_without_dead_letter_behavior() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-mandatory-policy-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-mandatory-policy-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-mandatory-policy-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "fail_run_on_failure",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription mandatory callback failure policy requires dead-letter or fallback behavior",
    }
    assert app.subscriptions("run-subscribe-mandatory-policy-1") == ()


def test_server_app_rejects_retrying_subscription_without_dead_letter_behavior() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-retry-policy-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-retry-policy-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-retry-policy-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "retry_then_dead_letter",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription retrying callback failure policy requires dead-letter or fallback behavior",
    }
    assert app.subscriptions("run-subscribe-retry-policy-1") == ()


def test_server_app_rejects_authoritative_event_subscription_projection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-subscribe-authoritative-1"] = ()

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-subscribe-authoritative-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-authoritative-invalid",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "sourceOfTruth": True,
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server event subscription callback delivery must not be used as the source of truth",
    }
    assert app.subscriptions("run-subscribe-authoritative-1") == ()


def test_server_app_rejects_subscription_with_invalid_event_filter_before_replay() -> None:
    for severity_min in ("panic", "error "):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id["run-subscribe-filter-invalid-1"] = ()

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/runs/run-subscribe-filter-invalid-1/subscriptions",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"sub-filter-invalid-{severity_min.strip()}",
                        "eventFilter": {"types": ["RunSucceeded"], "severityMin": severity_min},
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server event subscription event_filter.severity_min is invalid",
        }
        assert app.subscriptions("run-subscribe-filter-invalid-1") == ()


def test_server_app_rejects_subscription_with_invalid_visibility_filter() -> None:
    for visibility in ("public", "client "):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id["run-subscribe-visibility-invalid-1"] = ()

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/runs/run-subscribe-visibility-invalid-1/subscriptions",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"sub-visibility-invalid-{visibility.strip()}",
                        "eventFilter": {"types": ["RunSucceeded"], "visibility": [visibility]},
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server event subscription event_filter.visibility must contain only client, operator, internal, or audit_only",
        }
        assert app.subscriptions("run-subscribe-visibility-invalid-1") == ()


def test_server_app_rejects_subscription_with_whitespace_wrapped_identity_filters() -> None:
    cases = (
        (
            {"types": ["RunSucceeded "]},
            "server event subscription event_filter.types values must not contain surrounding whitespace",
        ),
        (
            {"types": ["RunSucceeded"], "nodeIds": ["runChecks "]},
            "server event subscription event_filter.node_ids values must not contain surrounding whitespace",
        ),
        (
            {"types": ["RunSucceeded"], "operationIds": ["op-ci-1 "]},
            "server event subscription event_filter.operation_ids values must not contain surrounding whitespace",
        ),
    )
    for index, (event_filter, expected_error) in enumerate(cases, start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id["run-subscribe-identity-invalid-1"] = ()

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/runs/run-subscribe-identity-invalid-1/subscriptions",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"sub-identity-invalid-{index}",
                        "eventFilter": event_filter,
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.subscriptions("run-subscribe-identity-invalid-1") == ()


def test_server_app_rejects_subscription_with_whitespace_wrapped_subscription_id() -> None:
    for index, subscription_id in enumerate((" sub-identity-invalid", "sub-identity-invalid "), start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        app._events_by_run_id[f"run-subscribe-id-invalid-{index}"] = ()

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/runs/run-subscribe-id-invalid-{index}/subscriptions",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": subscription_id,
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server event subscription subscription_id must not contain surrounding whitespace",
        }
        assert app.subscriptions(f"run-subscribe-id-invalid-{index}") == ()


def test_server_app_unsubscribes_without_dropping_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-unsubscribe"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Unsubscribe {message.text}"},
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
                    "runId": "run-unsubscribe-1",
                    "responseId": "response-unsubscribe-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-unsubscribe-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-unsubscribe-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-unsubscribe-1/subscriptions/sub-unsubscribe-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-unsubscribe-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-unsubscribe-1",
        "subscriptionId": "sub-unsubscribe-1",
        "status": "revoked",
    }
    assert app.subscriptions("run-unsubscribe-1")[0].status == "revoked"
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]


def test_server_app_treats_repeated_unsubscribe_as_idempotent() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-unsubscribe-idempotent"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Unsubscribe {message.text}"},
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
                    "runId": "run-unsubscribe-idempotent-1",
                    "responseId": "response-unsubscribe-idempotent-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-unsubscribe-idempotent-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-unsubscribe-idempotent-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    first = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-unsubscribe-idempotent-1/subscriptions/sub-unsubscribe-idempotent-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-unsubscribe-idempotent-1/subscriptions/sub-unsubscribe-idempotent-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-unsubscribe-idempotent-1",
        "subscriptionId": "sub-unsubscribe-idempotent-1",
        "status": "revoked",
        "duplicate": True,
    }
    assert len(app.subscriptions("run-unsubscribe-idempotent-1")) == 1
    assert app.subscriptions("run-unsubscribe-idempotent-1")[0].status == "revoked"


def test_server_app_rejects_unsubscribe_from_non_owner_principal() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1"),
                "other-token": PrincipalRef("user-2"),
            }
        )
    )
    app._events_by_run_id["run-unsubscribe-owner-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-unsubscribe-owner-1"},
            "metadata": {
                "runId": "run-unsubscribe-owner-1",
                "sequence": 1,
                "cursor": "run-unsubscribe-owner-1:1",
                "releaseId": "release-unsubscribe-owner-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )
    created = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-unsubscribe-owner-1/subscriptions",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-unsubscribe-owner-1",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-unsubscribe-owner-1/subscriptions/sub-unsubscribe-owner-1",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
        )
    )

    assert created.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": (
            "subscription 'sub-unsubscribe-owner-1' for run 'run-unsubscribe-owner-1' "
            "belongs to a different principal"
        ),
    }
    assert app.subscriptions("run-unsubscribe-owner-1")[0].status == "active"


def test_server_app_rejects_unsubscribe_from_same_principal_different_tenant() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1", tenant_id="tenant-a"),
                "other-token": PrincipalRef("user-1", tenant_id="tenant-b"),
            }
        )
    )
    app._events_by_run_id["run-unsubscribe-tenant-owner-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-unsubscribe-tenant-owner-1"},
            "metadata": {
                "runId": "run-unsubscribe-tenant-owner-1",
                "sequence": 1,
                "cursor": "run-unsubscribe-tenant-owner-1:1",
                "releaseId": "release-unsubscribe-tenant-owner-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )
    created = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-unsubscribe-tenant-owner-1/subscriptions",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-unsubscribe-tenant-owner-1",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-unsubscribe-tenant-owner-1/subscriptions/sub-unsubscribe-tenant-owner-1",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
        )
    )

    assert created.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": (
            "subscription 'sub-unsubscribe-tenant-owner-1' for run 'run-unsubscribe-tenant-owner-1' "
            "belongs to a different principal"
        ),
    }
    assert app.subscriptions("run-unsubscribe-tenant-owner-1")[0].status == "active"


def test_server_app_rejects_ack_after_subscription_is_revoked() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-revoked-ack"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Revoked {message.text}"},
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
                    "runId": "run-revoked-ack-1",
                    "responseId": "response-revoked-ack-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-revoked-ack-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-revoked-ack-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/run-revoked-ack-1/subscriptions/sub-revoked-ack-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-revoked-ack-1/subscriptions/sub-revoked-ack-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-revoked-ack-1:2"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-revoked-ack-1",
        "subscriptionId": "sub-revoked-ack-1",
        "state": "revoked",
        "error": "subscription 'sub-revoked-ack-1' for run 'run-revoked-ack-1' is revoked",
    }
    assert app.event_acks("run-revoked-ack-1", "sub-revoked-ack-1") == ()


def test_server_app_rejects_ack_from_non_owner_principal() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1"),
                "other-token": PrincipalRef("user-2"),
            }
        )
    )
    app._events_by_run_id["run-ack-owner-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-ack-owner-1"},
            "metadata": {
                "runId": "run-ack-owner-1",
                "sequence": 1,
                "cursor": "run-ack-owner-1:1",
                "releaseId": "release-ack-owner-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )
    created = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-owner-1/subscriptions",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-owner-1",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-owner-1/subscriptions/sub-ack-owner-1/ack",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-owner-1:1"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert created.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": "subscription 'sub-ack-owner-1' for run 'run-ack-owner-1' belongs to a different principal",
    }
    assert app.event_acks("run-ack-owner-1", "sub-ack-owner-1") == ()


def test_server_app_rejects_ack_from_same_principal_different_tenant() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1", tenant_id="tenant-a"),
                "other-token": PrincipalRef("user-1", tenant_id="tenant-b"),
            }
        )
    )
    app._events_by_run_id["run-ack-tenant-owner-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-ack-tenant-owner-1"},
            "metadata": {
                "runId": "run-ack-tenant-owner-1",
                "sequence": 1,
                "cursor": "run-ack-tenant-owner-1:1",
                "releaseId": "release-ack-tenant-owner-1",
                "occurredAt": "2026-07-03T00:00:00Z",
            },
        },
    )
    created = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-tenant-owner-1/subscriptions",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-tenant-owner-1",
                    "eventFilter": {"types": ["RunStarted"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-tenant-owner-1/subscriptions/sub-ack-tenant-owner-1/ack",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-tenant-owner-1:1"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    assert created.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": (
            "subscription 'sub-ack-tenant-owner-1' for run 'run-ack-tenant-owner-1' "
            "belongs to a different principal"
        ),
    }
    assert app.event_acks("run-ack-tenant-owner-1", "sub-ack-tenant-owner-1") == ()


def test_server_app_reports_missing_subscription_on_unsubscribe() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    missing_run = app.handle(
        ServerRequest(
            method="DELETE",
            path="/runs/missing-run/subscriptions/sub-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert missing_run.status_code == 404
    assert json.loads(missing_run.body.decode("utf-8")) == {
        "ok": False,
        "error": "run subscriptions not found for run 'missing-run'",
    }


def test_server_app_acknowledges_subscription_event_without_dropping_events() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-ack"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Ack {message.text}"},
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
                    "runId": "run-ack-1",
                    "responseId": "response-ack-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-1/subscriptions/sub-ack-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-1:2"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-ack-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-ack-1",
        "subscriptionId": "sub-ack-1",
        "eventId": "run-ack-1:run-terminal",
        "cursor": "run-ack-1:2",
        "status": "acknowledged",
    }
    assert app.event_acks("run-ack-1", "sub-ack-1") == (
        {
            "eventId": "run-ack-1:run-terminal",
            "cursor": "run-ack-1:2",
            "acknowledgedAt": "2026-07-02T00:00:00Z",
        },
    )
    with pytest.raises(TypeError):
        app.event_acks("run-ack-1", "sub-ack-1")[0]["cursor"] = "changed"
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]


def test_server_app_rejects_ack_cursor_for_different_run() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-cursor-scope-1"] = (
        {
            "kind": "RunSucceeded",
            "metadata": {
                "eventId": "evt-terminal",
                "runId": "run-ack-cursor-scope-1",
                "sequence": 2,
            },
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-cursor-scope-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-cursor-scope",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-cursor-scope-1/subscriptions/sub-ack-cursor-scope/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "other-run:2"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "ack request cursor must belong to run 'run-ack-cursor-scope-1'",
    }
    assert app.event_acks("run-ack-cursor-scope-1", "sub-ack-cursor-scope") == ()


def test_server_app_rejects_malformed_ack_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-cursor-format-1"] = (
        {
            "kind": "RunSucceeded",
            "metadata": {
                "eventId": "evt-terminal",
                "runId": "run-ack-cursor-format-1",
                "sequence": 2,
            },
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-cursor-format-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-cursor-format",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-cursor-format-1/subscriptions/sub-ack-cursor-format/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-cursor-format-1:not-a-sequence"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "ack request cursor must use '<run_id>:<sequence>' with a non-negative integer sequence",
    }
    assert app.event_acks("run-ack-cursor-format-1", "sub-ack-cursor-format") == ()


def test_server_app_rejects_ack_when_retained_event_sequence_is_malformed() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-bool-sequence-1"] = (
        {
            "kind": "RunSucceeded",
            "metadata": {
                "eventId": "evt-terminal",
                "runId": "run-ack-bool-sequence-1",
                "sequence": 2,
            },
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-bool-sequence-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-bool-sequence",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )
    app._events_by_run_id["run-ack-bool-sequence-1"][0]["metadata"]["sequence"] = True  # type: ignore[index]

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-bool-sequence-1/subscriptions/sub-ack-bool-sequence/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "evt-terminal"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "ack request sequence must be an integer",
    }
    assert app.event_acks("run-ack-bool-sequence-1", "sub-ack-bool-sequence") == ()


def test_server_app_deduplicates_repeated_subscription_ack_by_event_identity() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-ack-idempotent"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Ack {message.text}"},
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
                    "runId": "run-ack-idempotent-1",
                    "responseId": "response-ack-idempotent-1",
                }
            ).encode("utf-8"),
        )
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-idempotent-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-idempotent-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-idempotent-1/subscriptions/sub-ack-idempotent-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-idempotent-1:2"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-idempotent-1/subscriptions/sub-ack-idempotent-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "run-ack-idempotent-1:run-terminal"}).encode("utf-8"),
            requested_at="2026-07-02T00:00:05Z",
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "runId": "run-ack-idempotent-1",
        "subscriptionId": "sub-ack-idempotent-1",
        "eventId": "run-ack-idempotent-1:run-terminal",
        "cursor": "run-ack-idempotent-1:2",
        "status": "duplicate",
        "duplicate": True,
        "acknowledgedAt": "2026-07-02T00:00:00Z",
    }
    assert app.event_acks("run-ack-idempotent-1", "sub-ack-idempotent-1") == (
        {
            "eventId": "run-ack-idempotent-1:run-terminal",
            "cursor": "run-ack-idempotent-1:2",
            "acknowledgedAt": "2026-07-02T00:00:00Z",
        },
    )


def test_server_app_rejects_subscription_ack_with_invalid_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-invalid-time-1"] = (
        {
            "kind": "RunSucceeded",
            "metadata": {"eventId": "evt-terminal", "sequence": 1},
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-invalid-time-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-invalid-time-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-invalid-time-1/subscriptions/sub-ack-invalid-time-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "evt-terminal"}).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "ack request acknowledged_at must be an ISO datetime",
    }
    assert app.event_acks("run-ack-invalid-time-1", "sub-ack-invalid-time-1") == ()


def test_server_app_rejects_ack_with_conflicting_event_id_and_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-conflict-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
        {
            "kind": "RunSucceeded",
            "metadata": {"eventId": "evt-terminal", "sequence": 2},
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-conflict-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-conflict-1",
                    "eventFilter": {"types": ["RunStarted", "RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-conflict-1/subscriptions/sub-ack-conflict-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "evt-start", "cursor": "run-ack-conflict-1:2"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-ack-conflict-1",
        "subscriptionId": "sub-ack-conflict-1",
        "eventId": "evt-start",
        "cursor": "run-ack-conflict-1:2",
        "error": "ack event_id and cursor refer to different retained events",
    }
    assert app.event_acks("run-ack-conflict-1", "sub-ack-conflict-1") == ()


def test_server_app_rejects_ack_for_event_outside_subscription_filter() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-filter-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-start", "sequence": 1},
            "payload": {},
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "evt-progress", "sequence": 2},
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-filter-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-filter-1",
                    "eventFilter": {"types": ["JobProgress"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-filter-1/subscriptions/sub-ack-filter-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "evt-start"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-ack-filter-1",
        "subscriptionId": "sub-ack-filter-1",
        "eventId": "evt-start",
        "cursor": "run-ack-filter-1:1",
        "error": "acknowledged event is not selected by the subscription filter",
    }
    assert app.event_acks("run-ack-filter-1", "sub-ack-filter-1") == ()


def test_server_app_rejects_ack_for_hidden_or_malformed_visibility_event() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-ack-hidden-visibility-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "evt-invalid-visibility", "sequence": 1, "visibility": True},
            "payload": {},
        },
    )
    app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-hidden-visibility-1/subscriptions",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "sub-ack-hidden-visibility-1",
                    "eventFilter": {"types": ["JobProgress"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
        )
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-hidden-visibility-1/subscriptions/sub-ack-hidden-visibility-1/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"eventId": "evt-invalid-visibility"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 409
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "runId": "run-ack-hidden-visibility-1",
        "subscriptionId": "sub-ack-hidden-visibility-1",
        "eventId": "evt-invalid-visibility",
        "cursor": "run-ack-hidden-visibility-1:1",
        "error": "acknowledged event is not visible to the subscription principal",
    }
    assert app.event_acks("run-ack-hidden-visibility-1", "sub-ack-hidden-visibility-1") == ()


def test_server_app_rejects_ack_for_missing_event_or_subscription() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-ack-invalid"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Ack {message.text}"},
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
                    "runId": "run-ack-invalid-1",
                    "responseId": "response-ack-invalid-1",
                }
            ).encode("utf-8"),
        )
    )

    missing_subscription = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-ack-invalid-1/subscriptions/missing-sub/ack",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"cursor": "run-ack-invalid-1:2"}).encode("utf-8"),
        )
    )

    assert missing_subscription.status_code == 404
    assert json.loads(missing_subscription.body.decode("utf-8")) == {
        "ok": False,
        "error": "subscription 'missing-sub' not found for run 'run-ack-invalid-1'",
    }


def test_server_app_registers_and_revokes_callback_projection_with_run_replay() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-register-callback"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Register {message.text}"},
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
                    "runId": "run-register-callback-1",
                    "responseId": "response-register-callback-1",
                }
            ).encode("utf-8"),
        )
    )

    registered = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-1",
                    "scope": "run",
                    "scopeId": "run-register-callback-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                    "replayFromCursor": "run-register-callback-1:1",
                    "failurePolicy": "retry_then_dead_letter",
                    "deadLetterPolicy": "webhook-standard",
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )
    revoked = app.handle(
        ServerRequest(
            method="DELETE",
            path="/callbacks/callback-sub-1",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    events = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-register-callback-1/events",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    payload = json.loads(registered.body.decode("utf-8"))
    assert registered.status_code == 201
    assert payload["subscriptionId"] == "callback-sub-1"
    assert payload["scope"] == "run"
    assert payload["scopeId"] == "run-register-callback-1"
    assert payload["lastCursor"] == "run-register-callback-1:2"
    assert payload["owner"] == {
        "principalId": "user-1",
        "tenantId": None,
        "groups": [],
        "roles": [],
        "attributes": {},
    }
    assert payload["delivery"] == {
        "kind": "webhook",
        "url": "https://relay.example/events",
        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
    }
    assert [event["kind"] for event in payload["events"]] == ["RunSucceeded"]
    assert revoked.status_code == 202
    assert json.loads(revoked.body.decode("utf-8")) == {
        "ok": True,
        "subscriptionId": "callback-sub-1",
        "status": "revoked",
    }
    assert app.callback_registrations() == (
        ServerCallbackRegistration(
            subscription_id="callback-sub-1",
            scope="run",
            scope_id="run-register-callback-1",
            event_filter={"types": ("RunSucceeded",), "visibility": ("client",)},
            delivery={
                "kind": "webhook",
                "url": "https://relay.example/events",
                "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
            },
            status="revoked",
            failure_policy="retry_then_dead_letter",
            replay_from_cursor="run-register-callback-1:1",
            created_at="2026-07-02T00:00:00Z",
            owner=PrincipalRef("user-1"),
        ),
    )
    with pytest.raises(TypeError):
        app.callback_registrations()[0].delivery["signing"]["algorithm"] = "none"  # type: ignore[index]
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]


def test_server_app_constrains_callback_registration_visibility_to_principal_authority() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "client-token": PrincipalRef("user-1"),
                "operator-token": PrincipalRef("operator-1", roles=("operator",)),
            }
        )
    )
    app._events_by_run_id["run-register-callback-visibility-1"] = (
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-client", "sequence": 1},
            "payload": {"visibility": "client", "severity": "info"},
        },
        {
            "kind": "JobProgress",
            "metadata": {"eventId": "event-operator", "sequence": 2},
            "payload": {"visibility": "operator", "severity": "warning"},
        },
    )

    client_response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer client-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-client-visibility",
                    "scope": "run",
                    "scopeId": "run-register-callback-visibility-1",
                    "eventFilter": {"types": ["JobProgress"], "visibility": ["client", "operator"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )
    operator_response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer operator-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-operator-visibility",
                    "scope": "run",
                    "scopeId": "run-register-callback-visibility-1",
                    "eventFilter": {"types": ["JobProgress"], "visibility": ["client", "operator"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )

    client_payload = json.loads(client_response.body.decode("utf-8"))
    operator_payload = json.loads(operator_response.body.decode("utf-8"))
    assert client_response.status_code == 201
    assert operator_response.status_code == 201
    assert client_payload["eventFilter"]["visibility"] == ["client"]
    assert [event["metadata"]["eventId"] for event in client_payload["events"]] == ["event-client"]
    assert operator_payload["eventFilter"]["visibility"] == ["client", "operator"]
    assert [event["metadata"]["eventId"] for event in operator_payload["events"]] == [
        "event-client",
        "event-operator",
    ]
    assert dict(app.callback_registrations()[0].event_filter) == {
        "types": ("JobProgress",),
        "visibility": ("client",),
    }


def test_server_app_callback_registration_filters_async_events_by_metadata() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "relay-token": PrincipalRef("callback-relay", roles=("operator",)),
                "operator-token": PrincipalRef("operator-1", roles=("operator",)),
            }
        )
    )
    app._events_by_run_id["run-callback-filter-metadata-1"] = (
        {
            "kind": "RunStarted",
            "payload": {"runId": "run-callback-filter-metadata-1"},
            "metadata": {
                "eventId": "run-callback-filter-metadata-1:run-started",
                "runId": "run-callback-filter-metadata-1",
                "sequence": 1,
                "cursor": "run-callback-filter-metadata-1:1",
                "releaseId": "release-callback-filter-metadata-1",
                "occurredAt": "2026-07-03T00:00:00Z",
                "visibility": "client",
            },
        },
    )

    callback = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/op-ci-filter-metadata-1",
            headers={"Authorization": "Bearer relay-token", "GraphBlocks-Idempotency-Key": "idem-filter-metadata-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "callbackId": "cb-filter-metadata-1",
                    "runId": "run-callback-filter-metadata-1",
                    "nodeId": "waitCI",
                    "attemptId": "attempt-1",
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:01Z",
        )
    )
    registered = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer operator-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-filter-metadata-1",
                    "scope": "run",
                    "scopeId": "run-callback-filter-metadata-1",
                    "eventFilter": {
                        "types": ["ExternalCallbackReceived"],
                        "visibility": ["operator"],
                        "operationIds": ["op-ci-filter-metadata-1"],
                        "nodeIds": ["waitCI"],
                    },
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                    "replayFromCursor": "run-callback-filter-metadata-1:1",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:02Z",
        )
    )

    payload = json.loads(registered.body.decode("utf-8"))
    assert callback.status_code == 202
    assert registered.status_code == 201
    assert payload["lastCursor"] == "run-callback-filter-metadata-1:2"
    assert [event["kind"] for event in payload["events"]] == ["ExternalCallbackReceived"]
    event = payload["events"][0]
    assert event["metadata"]["visibility"] == "operator"
    assert event["metadata"]["operationId"] == "op-ci-filter-metadata-1"
    assert event["metadata"]["nodeId"] == "waitCI"
    assert "operationId" not in event["payload"]
    assert "nodeId" not in event["payload"]


def test_server_app_rejects_duplicate_callback_registration_id_without_overwrite() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-duplicate",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-duplicate",
                    "scope": "tenant",
                    "scopeId": "tenant-2",
                    "eventFilter": {"types": ["RunFailed"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://other-relay.example/events",
                        "signing": {"algorithm": "ed25519", "secret_ref": "secret://other-relay"},
                    },
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:05Z",
        )
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": False,
        "subscriptionId": "callback-sub-duplicate",
        "state": "active",
        "error": "callback registration 'callback-sub-duplicate' already exists",
    }
    assert app.callback_registrations() == (
        ServerCallbackRegistration(
            subscription_id="callback-sub-duplicate",
            scope="tenant",
            scope_id="tenant-1",
            event_filter={"types": ("RunSucceeded",), "visibility": ("client",)},
            delivery={
                "kind": "webhook",
                "url": "https://relay.example/events",
                "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
            },
            failure_policy="retry_then_dead_letter",
            created_at="2026-07-03T00:00:00Z",
            owner=PrincipalRef("user-1"),
        ),
    )


def test_server_app_rejects_callback_registration_for_different_principal_tenant_scope() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1", tenant_id="tenant-a")})
    )

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-tenant-scope-auth",
                    "scope": "tenant",
                    "scopeId": "tenant-b",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 403
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback registration tenant scope 'tenant-b' is not allowed for principal tenant 'tenant-a'",
    }
    assert app.callback_registrations() == ()


def test_server_app_treats_repeated_callback_revoke_as_idempotent() -> None:
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
                    "subscriptionId": "callback-sub-revoke-idempotent",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    first = app.handle(
        ServerRequest(
            method="DELETE",
            path="/callbacks/callback-sub-revoke-idempotent",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="DELETE",
            path="/callbacks/callback-sub-revoke-idempotent",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "subscriptionId": "callback-sub-revoke-idempotent",
        "status": "revoked",
        "duplicate": True,
    }
    assert app.callback_registrations()[0].status == "revoked"


def test_server_app_rejects_callback_revoke_from_non_owner_principal() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1"),
                "other-token": PrincipalRef("user-2"),
            }
        )
    )
    registered = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-owner-1",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="DELETE",
            path="/callbacks/callback-sub-owner-1",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
        )
    )

    assert registered.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback registration 'callback-sub-owner-1' belongs to a different principal",
    }
    assert app.callback_registrations()[0].status == "active"


def test_server_app_rejects_callback_revoke_from_same_principal_different_tenant() -> None:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "owner-token": PrincipalRef("user-1", tenant_id="tenant-a"),
                "other-token": PrincipalRef("user-1", tenant_id="tenant-b"),
            }
        )
    )
    registered = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer owner-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-tenant-owner-1",
                    "scope": "tenant",
                    "scopeId": "tenant-a",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    denied = app.handle(
        ServerRequest(
            method="DELETE",
            path="/callbacks/callback-sub-tenant-owner-1",
            headers={"Authorization": "Bearer other-token"},
            query={},
            cookies={},
        )
    )

    assert registered.status_code == 201
    assert denied.status_code == 403
    assert json.loads(denied.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback registration 'callback-sub-tenant-owner-1' belongs to a different principal",
    }
    assert app.callback_registrations()[0].status == "active"


def test_server_app_registers_callback_projection_from_accepted_run_initial_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-register-callback-initial"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Callback {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    accepted = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph,
                    "inputs": {"message": {"text": "initial"}},
                    "runId": "run-register-initial-1",
                    "responseId": "response-register-initial-1",
                    "responseMode": "accepted",
                }
            ).encode("utf-8"),
        )
    )
    initial_cursor = json.loads(accepted.body.decode("utf-8"))["initialCursor"]

    registered = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-initial",
                    "scope": "run",
                    "scopeId": "run-register-initial-1",
                    "eventFilter": {"types": ["RunStarted", "RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                    "replayFromCursor": initial_cursor,
                }
            ).encode("utf-8"),
        )
    )

    payload = json.loads(registered.body.decode("utf-8"))
    assert registered.status_code == 201
    assert payload["replayFromCursor"] == "run-register-initial-1:0"
    assert payload["lastCursor"] == "run-register-initial-1:2"
    assert [event["kind"] for event in payload["events"]] == ["RunStarted", "RunSucceeded"]
    assert payload["events"][1]["payload"]["outputs"] == {"prompt": "Callback initial"}


def test_server_app_rejects_callback_registration_for_missing_run_scope() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-missing",
                    "scope": "run",
                    "scopeId": "missing-run",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run event stream not found for callback registration scope 'missing-run'",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_invalid_scope() -> None:
    for scope in ("workspace", "tenant "):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-invalid-scope-{scope.strip()}",
                        "scope": scope,
                        "scopeId": "workspace-1",
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": {
                            "kind": "webhook",
                            "url": "https://relay.example/events",
                            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                        },
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server callback registration scope must be one of run, conversation, project, tenant, or deployment",
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_invalid_failure_policy() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-invalid-policy",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "webhook", "url": "https://relay.example/events"},
                    "failurePolicy": "retry_forever",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server subscription failure_policy must be one of best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_invalid_created_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-created-time-invalid",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration created_at must be an ISO datetime",
    }
    assert app.callback_registrations() == ()

    compact_offset = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-created-time-compact-offset",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00+0000",
        )
    )

    assert compact_offset.status_code == 400
    assert json.loads(compact_offset.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration created_at must be an ISO datetime",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_mandatory_callback_registration_without_retry_or_dead_letter_policy() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-mandatory-invalid",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "mandatory": True,
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "best_effort",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration mandatory delivery requires retry, dead-letter, pause-run, or fail-run failure policy",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_mandatory_callback_registration_failure_policy_without_dead_letter_behavior() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-mandatory-policy-invalid",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "fail_run_on_failure",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration mandatory callback failure policy requires dead-letter or fallback behavior",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_retrying_callback_registration_without_dead_letter_behavior() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-retry-policy-invalid",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "retry_then_dead_letter",
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration retrying callback failure policy requires dead-letter or fallback behavior",
    }
    assert app.callback_registrations() == ()


def test_server_app_accepts_mandatory_callback_registration_failure_policy_with_fallback_behavior() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-mandatory-policy-fallback",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "failurePolicy": "fail_run_on_failure",
                    "fallbackPolicy": "operator_review",
                }
            ).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 201
    assert json.loads(response.body.decode("utf-8"))["subscriptionId"] == "callback-sub-mandatory-policy-fallback"
    assert app.callback_registrations()[0].failure_policy == "fail_run_on_failure"


def test_server_app_rejects_authoritative_callback_registration_projection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-authoritative-invalid",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    "authoritativeFor": ["billing"],
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration callback delivery must not be used as the source of truth",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_webhook_callback_registration_without_signing() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-missing-signing",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {"kind": "webhook", "url": "https://relay.example/events"},
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration delivery.signing must be a mapping for webhook delivery",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_non_post_webhook_callback_registration_method() -> None:
    for method in ("GET", "post"):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-webhook-{method.lower()}",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": {
                            "kind": "webhook",
                            "url": "https://relay.example/events",
                            "method": method,
                            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                        },
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server callback registration delivery.method must be POST for webhook delivery",
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_whitespace_wrapped_webhook_callback_registration_literals() -> None:
    cases = (
        (
            "kind",
            "webhook ",
            "server callback registration delivery.kind must be one of webhook, websocket, sse, push_notification, email, or local_callback",
        ),
        (
            "method",
            "POST ",
            "server callback registration delivery.method must be POST for webhook delivery",
        ),
        (
            "algorithm",
            "hmac-sha256 ",
            "server callback registration delivery.signing.algorithm must be one of hmac-sha256 or ed25519",
        ),
    )
    for field_name, value, expected_error in cases:
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
        delivery = {
            "kind": "webhook",
            "url": "https://relay.example/events",
            "method": "POST",
            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
        }
        if field_name == "algorithm":
            delivery["signing"] = {"algorithm": value, "secret_ref": "secret://relay"}
        else:
            delivery[field_name] = value

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-webhook-whitespace-{field_name}",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": delivery,
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_unsafe_webhook_callback_registration_target() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-unsafe-target",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "http://127.0.0.1:9000/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration delivery.url is unsafe or forbidden by default egress policy",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_whitespace_wrapped_webhook_callback_registration_target() -> None:
    for url in (" https://relay.example/events", "https://relay.example/events "):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": "callback-sub-whitespace-target",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": {
                            "kind": "webhook",
                            "url": url,
                            "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                        },
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server callback registration delivery.url is unsafe or forbidden by default egress policy",
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_webhook_callback_registration_target_with_userinfo() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-userinfo-target",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "webhook",
                        "url": "https://relay-user:relay-pass@relay.example/events",
                        "signing": {"algorithm": "hmac-sha256", "secret_ref": "secret://relay"},
                    },
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration delivery.url is unsafe or forbidden by default egress policy",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_impossible_ordered_callback_registration_delivery() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/register",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "subscriptionId": "callback-sub-ordering",
                    "scope": "tenant",
                    "scopeId": "tenant-1",
                    "eventFilter": {"types": ["RunSucceeded"]},
                    "delivery": {
                        "kind": "local_callback",
                        "callback_name": "ide",
                        "ordering": {"scope": "run", "mode": "ordered"},
                    },
                }
            ).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "server callback registration delivery.ordering requests ordered delivery on an unsupported target",
    }
    assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_invalid_event_filter_before_replay() -> None:
    invalid_filters = (
        ({"types": ["RunSucceeded"], "includeTerminalEvents": "yes"}, "include_terminal_events must be a boolean"),
        ({"types": ["RunSucceeded"], "severityMin": "error "}, "severity_min is invalid"),
    )
    for event_filter, error_fragment in invalid_filters:
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-invalid-filter-{len(error_fragment)}",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": event_filter,
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert error_fragment in json.loads(response.body.decode("utf-8"))["error"]
        assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_invalid_visibility_filter() -> None:
    for visibility in ("public", "operator "):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-invalid-visibility-{visibility.strip()}",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": {"types": ["RunSucceeded"], "visibility": [visibility]},
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": "server event subscription event_filter.visibility must contain only client, operator, internal, or audit_only",
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_whitespace_wrapped_identity_filters() -> None:
    cases = (
        (
            {"types": ["RunSucceeded "]},
            "server event subscription event_filter.types values must not contain surrounding whitespace",
        ),
        (
            {"types": ["RunSucceeded"], "nodeIds": ["waitCI "]},
            "server event subscription event_filter.node_ids values must not contain surrounding whitespace",
        ),
        (
            {"types": ["RunSucceeded"], "operationIds": ["op-ci-filter-metadata-1 "]},
            "server event subscription event_filter.operation_ids values must not contain surrounding whitespace",
        ),
    )
    for index, (event_filter, expected_error) in enumerate(cases, start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": f"callback-sub-identity-invalid-{index}",
                        "scope": "tenant",
                        "scopeId": "tenant-1",
                        "eventFilter": event_filter,
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.callback_registrations() == ()


def test_server_app_rejects_callback_registration_with_whitespace_wrapped_identity_fields() -> None:
    cases = (
        (
            {"subscriptionId": " callback-sub-identity-invalid", "scopeId": "tenant-1"},
            "server callback registration subscription_id must not contain surrounding whitespace",
        ),
        (
            {"subscriptionId": "callback-sub-identity-invalid", "scopeId": "tenant-1 "},
            "server callback registration scope_id must not contain surrounding whitespace",
        ),
    )
    for body_overrides, expected_error in cases:
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        **body_overrides,
                        "scope": "tenant",
                        "eventFilter": {"types": ["RunSucceeded"]},
                        "delivery": {"kind": "local_callback", "callback_name": "ide"},
                    }
                ).encode("utf-8"),
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.callback_registrations() == ()


def test_server_app_records_callback_delivery_redrive_and_dead_letter_projection() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    redrive = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-1/redrive",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-1", "reason": "receiver recovered"}).encode("utf-8"),
            requested_at="2026-07-02T00:02:00Z",
        )
    )
    dead_letter = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-1/dead-letter",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-1", "reason": "max attempts exhausted"}).encode("utf-8"),
            requested_at="2026-07-02T00:03:00Z",
        )
    )

    assert redrive.status_code == 202
    assert json.loads(redrive.body.decode("utf-8")) == {
        "ok": True,
        "deliveryId": "del-1",
        "operator": "operator-1",
        "reason": "receiver recovered",
        "status": "redrive_requested",
    }
    assert dead_letter.status_code == 202
    assert json.loads(dead_letter.body.decode("utf-8")) == {
        "ok": True,
        "deliveryId": "del-1",
        "operator": "operator-1",
        "reason": "max attempts exhausted",
        "status": "dead_letter_requested",
    }
    assert app.callback_delivery_redrives("del-1") == (
        {
            "deliveryId": "del-1",
            "operator": "operator-1",
            "reason": "receiver recovered",
            "requestedAt": "2026-07-02T00:02:00Z",
            "status": "redrive_requested",
        },
    )
    assert app.callback_delivery_dead_letter_moves("del-1") == (
        {
            "deliveryId": "del-1",
            "operator": "operator-1",
            "reason": "max attempts exhausted",
            "requestedAt": "2026-07-02T00:03:00Z",
            "status": "dead_letter_requested",
        },
    )
    with pytest.raises(TypeError):
        app.callback_delivery_redrives("del-1")[0]["reason"] = "changed"
    with pytest.raises(TypeError):
        app.callback_delivery_dead_letter_moves("del-1")[0]["reason"] = "changed"


def test_server_app_uses_authenticated_principal_for_callback_delivery_control_operator() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-authenticated/redrive",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "receiver recovered"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:00Z",
        )
    )

    assert response.status_code == 202
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": True,
        "deliveryId": "del-authenticated",
        "operator": "operator-1",
        "reason": "receiver recovered",
        "status": "redrive_requested",
    }
    assert app.callback_delivery_redrives("del-authenticated") == (
        {
            "deliveryId": "del-authenticated",
            "operator": "operator-1",
            "reason": "receiver recovered",
            "requestedAt": "2026-07-03T00:00:00Z",
            "status": "redrive_requested",
        },
    )


def test_server_app_rejects_callback_delivery_control_operator_mismatch() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-forged/redrive",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-2", "reason": "receiver recovered"}).encode("utf-8"),
            requested_at="2026-07-03T00:00:30Z",
        )
    )

    assert response.status_code == 403
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback delivery control request operator must match authenticated principal",
    }
    assert app.callback_delivery_redrives("del-forged") == ()


def test_server_app_treats_repeated_callback_dead_letter_move_as_idempotent() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    first = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-idempotent/dead-letter",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-1", "reason": "max attempts exhausted"}).encode("utf-8"),
            requested_at="2026-07-03T00:01:00Z",
        )
    )
    duplicate = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-idempotent/dead-letter",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"reason": "already moved"}).encode("utf-8"),
            requested_at="2026-07-03T00:02:00Z",
        )
    )

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "deliveryId": "del-idempotent",
        "operator": "operator-1",
        "reason": "max attempts exhausted",
        "status": "dead_letter_requested",
        "requestedAt": "2026-07-03T00:01:00Z",
        "duplicate": True,
    }
    assert app.callback_delivery_dead_letter_moves("del-idempotent") == (
        {
            "deliveryId": "del-idempotent",
            "operator": "operator-1",
            "reason": "max attempts exhausted",
            "requestedAt": "2026-07-03T00:01:00Z",
            "status": "dead_letter_requested",
        },
    )


def test_server_app_rejects_malformed_callback_delivery_control_request() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-1/redrive",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-1", "reason": " "}).encode("utf-8"),
            requested_at="2026-07-02T00:04:00Z",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback delivery control request reason must not be empty",
    }
    assert app.callback_delivery_redrives("del-1") == ()


def test_server_app_rejects_callback_delivery_control_with_whitespace_wrapped_operator_or_reason() -> None:
    cases = (
        (
            {"operator": "operator-1 ", "reason": "receiver recovered"},
            "callback delivery control request operator must not contain surrounding whitespace",
        ),
        (
            {"operator": "operator-1", "reason": " receiver recovered"},
            "callback delivery control request reason must not contain surrounding whitespace",
        ),
    )
    for index, (body, expected_error) in enumerate(cases, start=1):
        app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/callbacks/deliveries/del-whitespace-{index}/redrive",
                headers={"Authorization": "Bearer token-1"},
                query={},
                cookies={},
                body=json.dumps(body).encode("utf-8"),
                requested_at="2026-07-02T00:04:00Z",
            )
        )

        assert response.status_code == 400
        assert json.loads(response.body.decode("utf-8")) == {
            "ok": False,
            "error": expected_error,
        }
        assert app.callback_delivery_redrives(f"del-whitespace-{index}") == ()


def test_server_app_rejects_callback_delivery_control_with_invalid_timestamp() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("operator-1")}))

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/callbacks/deliveries/del-invalid-time/redrive",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
            body=json.dumps({"operator": "operator-1", "reason": "retry requested"}).encode("utf-8"),
            requested_at="not-a-date",
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "callback delivery control request requested_at must be an ISO datetime",
    }
    assert app.callback_delivery_redrives("del-invalid-time") == ()


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

    status_response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/missing-run",
            headers={"Authorization": "Bearer token-1"},
            query={},
            cookies={},
        )
    )

    assert status_response.status_code == 404
    assert json.loads(status_response.body.decode("utf-8")) == {
        "ok": False,
        "error": "run status not found for run 'missing-run'",
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


def test_server_app_rejects_boolean_event_sequence_for_stream_cursor() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1")}))
    app._events_by_run_id["run-stream-bool-sequence-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {"eventId": "evt-bool", "sequence": True},
            "payload": {},
        },
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-stream-bool-sequence-1/stream",
            headers={
                "Authorization": "Bearer token-1",
                "Connection": "Upgrade",
                "Upgrade": "websocket",
            },
            query={},
            cookies={},
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body.decode("utf-8")) == {
        "ok": False,
        "error": "application stream sequence must be an integer",
    }
