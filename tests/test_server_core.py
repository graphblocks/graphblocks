from __future__ import annotations

import json

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

    endpoint = default_server_route_manifest().lookup("POST", "/runs/{run_id}/cancel")
    with pytest.raises(ValueError, match="server route path_params must be a mapping"):
        ServerRouteMatch(endpoint, path_params=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={" ": "run-123"})
    with pytest.raises(ValueError, match="server route path_params keys and values must be strings"):
        ServerRouteMatch(endpoint, path_params={"run_id": object()})  # type: ignore[dict-item]


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


def test_server_app_accepts_authenticated_async_callback_submission() -> None:
    app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("callback-relay")}))

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
                    "payload": {"status": "completed"},
                }
            ).encode("utf-8"),
            requested_at="2026-07-02T00:00:00Z",
        )
    )

    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 202
    assert payload == {
        "ok": True,
        "operationId": "op-ci-1",
        "callbackId": "cb-1",
        "idempotencyKey": "idem-callback-1",
        "status": "accepted",
    }
    assert app.callback_submissions("op-ci-1") == (
        ServerAsyncCallbackSubmission(
            operation_id="op-ci-1",
            callback_id="cb-1",
            idempotency_key="idem-callback-1",
            payload={"status": "completed"},
            run_id="run-1",
            node_id="waitCI",
            attempt_id="attempt-1",
            provider_operation_id=None,
            received_at="2026-07-02T00:00:00Z",
        ),
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
                "run_id": "run-1",
                "node_id": "waitCI",
                "payload": {"status": "completed"},
            }
        ).encode("utf-8"),
        requested_at="2026-07-02T00:00:00Z",
    )

    first = app.handle(request)
    duplicate = app.handle(request)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert json.loads(duplicate.body.decode("utf-8")) == {
        "ok": True,
        "operationId": "op-ci-1",
        "callbackId": "cb-1",
        "idempotencyKey": "idem-callback-1",
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

    assert statuses == [202, 202, 200, 202, 200, 200]
    assert [submission.idempotency_key for submission in app.callback_submissions("op-ci-1")] == [
        "idem-callback-1",
        "idem-callback-2",
        "idem-callback-3",
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
    assert events.status_code == 200
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]
    assert json.loads(status.body.decode("utf-8"))["state"] == "succeeded"


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
                    "delivery": {"kind": "local_callback", "callback_name": "ide"},
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
    assert payload["eventFilter"] == {"types": ["RunSucceeded"]}
    assert payload["delivery"] == {"kind": "local_callback", "callback_name": "ide"}
    assert [event["kind"] for event in payload["events"]] == ["RunSucceeded"]
    assert app.subscriptions("run-subscribe-1") == (
        ServerEventSubscription(
            subscription_id="sub-run-1",
            run_id="run-subscribe-1",
            event_filter={"types": ["RunSucceeded"]},
            delivery={"kind": "local_callback", "callback_name": "ide"},
            status="active",
            failure_policy="best_effort",
            replay_from_cursor="run-subscribe-1:1",
            created_at="2026-07-02T00:00:00Z",
        ),
    )


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
    }
    assert app.subscriptions("run-subscribe-expired-1") == ()


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
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]


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
                    "delivery": {"kind": "webhook", "url": "https://relay.example/events"},
                    "replayFromCursor": "run-register-callback-1:1",
                    "failurePolicy": "retry_then_dead_letter",
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
            event_filter={"types": ["RunSucceeded"]},
            delivery={"kind": "webhook", "url": "https://relay.example/events"},
            status="revoked",
            failure_policy="retry_then_dead_letter",
            replay_from_cursor="run-register-callback-1:1",
            created_at="2026-07-02T00:00:00Z",
        ),
    )
    assert [event["kind"] for event in json.loads(events.body.decode("utf-8"))["events"]] == [
        "RunStarted",
        "RunSucceeded",
    ]


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
                    "delivery": {"kind": "webhook", "url": "https://relay.example/events"},
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
