from __future__ import annotations

import json

import graphblocks.server as graphblocks_server

from graphblocks.policy import PrincipalRef
from graphblocks.server import (
    GraphBlocksServerApp,
    ServerAsyncCallbackSubmission,
    ServerAuthorizationDecision,
    ServerAuthorizationRequest,
    ServerCallbackDeliveryResult,
    ServerCallbackRegistration,
    ServerEndpoint,
    ServerEventSubscription,
    ServerRequest,
    StaticBearerAuthHook,
    default_server_route_manifest,
)


_RUN_ID = "run-authorization-matrix"
_EVENT_SUBSCRIPTION_ID = "event-subscription-authorization-matrix"
_CALLBACK_SUBSCRIPTION_ID = "callback-subscription-authorization-matrix"
_DELIVERY_ID = "delivery-authorization-matrix"
_OPERATION_ID = "operation-authorization-matrix"
_OWNER = PrincipalRef("alice", tenant_id="tenant-a")
_IDENTITIES = (
    ("owner", _OWNER),
    ("same-principal-other-tenant", PrincipalRef("alice", tenant_id="tenant-b")),
    ("other-principal-same-tenant", PrincipalRef("bob", tenant_id="tenant-a")),
    ("other-principal-other-tenant", PrincipalRef("bob", tenant_id="tenant-b")),
)
_SUPPORTED_RESOURCE_KINDS = {
    "callback_delivery",
    "callback_operation",
    "callback_subscription",
    "event_subscription",
    "run",
}


def _protected_endpoints() -> tuple[ServerEndpoint, ...]:
    return tuple(
        endpoint
        for endpoint in default_server_route_manifest().endpoints
        if endpoint.auth_required
    )


def _resource_policy(endpoint: ServerEndpoint) -> tuple[str, str] | None:
    return graphblocks_server._SERVER_AUTHORIZATION_RESOURCE_POLICIES.get(
        endpoint.operation
    )


def _endpoint_id(endpoint: ServerEndpoint) -> str:
    return "-".join(
        (
            endpoint.operation,
            endpoint.transport,
            endpoint.path.strip("/").replace("/", "-") or "root",
        )
    )


def _authorization_cases() -> tuple[tuple[ServerEndpoint, str], ...]:
    return tuple(
        (endpoint, resource_state)
        for endpoint in _protected_endpoints()
        for resource_state in (
            ("accepted", "running")
            if _resource_policy(endpoint) is not None
            else ("principal",)
        )
    )


class _OwnerAuthorizer:
    def __init__(self) -> None:
        self.observed: list[tuple[ServerAuthorizationRequest, bool]] = []

    def authorize(
        self,
        request: ServerAuthorizationRequest,
    ) -> ServerAuthorizationDecision:
        resource = request.resource
        allowed = resource is None or (
            resource.tenant_id == request.principal.tenant_id
            and resource.attributes.get("ownerPrincipalId")
            == request.principal.principal_id
        )
        self.observed.append((request, allowed))
        if allowed:
            return ServerAuthorizationDecision(True)
        return ServerAuthorizationDecision(
            False,
            reason_codes=("authz.owner_mismatch",),
            hide_resource=True,
        )


def _seed_authorization_resources(
    app: GraphBlocksServerApp,
    resource_state: str,
) -> None:
    app._record_run_authorization(
        _RUN_ID,
        _OWNER,
        "2026-08-08T00:00:00Z",
    )
    event_kind = "RunAccepted" if resource_state == "accepted" else "RunStarted"
    app._events_by_run_id[_RUN_ID] = (
        {
            "kind": event_kind,
            "metadata": {
                "eventId": f"event-{resource_state}",
                "runId": _RUN_ID,
                "sequence": 1,
                "cursor": f"{_RUN_ID}:1",
                "releaseId": "release-authorization-matrix",
                "occurredAt": "2026-08-08T00:00:00Z",
            },
            "payload": {},
        },
    )
    if resource_state == "accepted":
        app._pending_accepted_runs_by_run_id[_RUN_ID] = {
            "responseId": "response-authorization-matrix",
            "releaseId": "release-authorization-matrix",
            "policySnapshotId": "policy-authorization-matrix",
            "turnId": None,
        }

    app._subscriptions_by_run_id[_RUN_ID] = [
        ServerEventSubscription(
            subscription_id=_EVENT_SUBSCRIPTION_ID,
            run_id=_RUN_ID,
            event_filter={},
            delivery={"kind": "local_callback", "callback_name": "matrix"},
            created_at="2026-08-08T00:00:00Z",
            owner=_OWNER,
        )
    ]
    app._callback_registrations[_CALLBACK_SUBSCRIPTION_ID] = ServerCallbackRegistration(
        subscription_id=_CALLBACK_SUBSCRIPTION_ID,
        scope="tenant",
        scope_id="tenant-a",
        event_filter={},
        delivery={"kind": "local_callback", "callback_name": "matrix"},
        created_at="2026-08-08T00:00:00Z",
        owner=_OWNER,
    )
    app._callback_delivery_results_by_subscription_id[_CALLBACK_SUBSCRIPTION_ID] = [
        ServerCallbackDeliveryResult(
            delivery_id=_DELIVERY_ID,
            subscription_id=_CALLBACK_SUBSCRIPTION_ID,
            event_id="callback-event-authorization-matrix",
            run_id=_RUN_ID,
            sequence=1,
            cursor=f"{_RUN_ID}:1",
            attempt=1,
            idempotency_key="callback-subscription-authorization-matrix:event-1",
            status="failed",
            status_code=503,
            last_error="receiver unavailable",
        )
    ]
    app._callbacks_by_operation_id[_OPERATION_ID] = [
        ServerAsyncCallbackSubmission(
            operation_id=_OPERATION_ID,
            callback_id="callback-authorization-matrix",
            idempotency_key="callback-operation-authorization-matrix",
            payload={"status": "pending"},
            run_id=_RUN_ID,
            received_at="2026-08-08T00:00:00Z",
            verified_by="matrix-test",
        )
    ]


def _request_for_endpoint(
    endpoint: ServerEndpoint,
    *,
    token: str,
) -> ServerRequest:
    policy = _resource_policy(endpoint)
    resource_kind = policy[1] if policy is not None else None
    parameter_values = {
        "run_id": _RUN_ID,
        "subscription_id": (
            _EVENT_SUBSCRIPTION_ID
            if resource_kind == "event_subscription"
            else _CALLBACK_SUBSCRIPTION_ID
        ),
        "delivery_id": _DELIVERY_ID,
        "operation_id": _OPERATION_ID,
    }
    path = endpoint.path
    for parameter, value in parameter_values.items():
        path = path.replace("{" + parameter + "}", value)
    headers = {"authorization": f"Bearer {token}"}
    if endpoint.transport == "sse":
        headers["accept"] = "text/event-stream"
    elif endpoint.transport == "websocket":
        headers["upgrade"] = "websocket"
    return ServerRequest(
        method=endpoint.method,
        path=path,
        headers=headers,
        query={},
        cookies={},
        body=b"{}",
        requested_at="2026-08-08T00:00:01Z",
    )


def test_protected_route_manifest_is_fully_covered_by_authorization_matrix() -> None:
    protected = _protected_endpoints()
    covered = {endpoint for endpoint, _state in _authorization_cases()}

    assert covered == set(protected)
    assert {
        policy[1]
        for endpoint in protected
        if (policy := _resource_policy(endpoint)) is not None
    } == _SUPPORTED_RESOURCE_KINDS
    for endpoint in protected:
        path_parameters = {
            part[1:-1]
            for part in endpoint.path.strip("/").split("/")
            if part.startswith("{") and part.endswith("}")
        }
        policy = _resource_policy(endpoint)
        if path_parameters:
            assert policy is not None, endpoint.operation
            assert policy[0] in path_parameters, endpoint.operation
        else:
            assert policy is None, endpoint.operation


def test_server_protected_route_authorization_matrix() -> None:
    for identity_name, principal in _IDENTITIES:
        for endpoint, resource_state in _authorization_cases():
            case = f"{identity_name}:{_endpoint_id(endpoint)}:{resource_state}"
            authorizer = _OwnerAuthorizer()
            token = f"token-{identity_name}"
            app = GraphBlocksServerApp(
                auth_hook=StaticBearerAuthHook({token: principal}),
                authorization_hook=authorizer,
                allow_unsafe_multi_tenant_dev=True,
            )
            _seed_authorization_resources(
                app,
                ("running" if resource_state == "principal" else resource_state),
            )

            response = app.handle(_request_for_endpoint(endpoint, token=token))

            assert len(authorizer.observed) == 1, case
            authorization_request, allowed = authorizer.observed[0]
            assert authorization_request.route == endpoint, case
            assert authorization_request.action == endpoint.operation, case
            assert authorization_request.principal == principal, case
            policy = _resource_policy(endpoint)
            if policy is None:
                assert authorization_request.resource is None, case
                assert allowed is True, case
                assert response.status_code not in {401, 403}, case
                continue

            resource = authorization_request.resource
            assert resource is not None, case
            assert resource.resource_kind == policy[1], case
            assert resource.tenant_id == _OWNER.tenant_id, case
            assert resource.attributes["ownerPrincipalId"] == _OWNER.principal_id, case
            if identity_name == "owner":
                assert allowed is True, case
                assert response.status_code not in {401, 403}, case
            else:
                assert allowed is False, case
                assert response.status_code == 404, case
                assert json.loads(response.body) == {
                    "ok": False,
                    "reasonCodes": ["authz.resource_hidden"],
                }, case


def test_server_run_list_filters_the_full_identity_matrix() -> None:
    for resource_state in ("accepted", "running"):
        tokens = {
            f"token-{identity_name}": principal
            for identity_name, principal in _IDENTITIES
        }
        app = GraphBlocksServerApp(
            auth_hook=StaticBearerAuthHook(tokens),
            authorization_hook=_OwnerAuthorizer(),
            allow_unsafe_multi_tenant_dev=True,
        )
        expected_run_by_identity: dict[str, str] = {}
        for index, (identity_name, principal) in enumerate(
            _IDENTITIES,
            start=1,
        ):
            run_id = f"run-{identity_name}"
            expected_run_by_identity[identity_name] = run_id
            app._record_run_authorization(
                run_id,
                principal,
                f"2026-08-08T00:00:0{index}Z",
            )
            event_kind = "RunAccepted" if resource_state == "accepted" else "RunStarted"
            app._events_by_run_id[run_id] = (
                {
                    "kind": event_kind,
                    "metadata": {
                        "eventId": f"event-{identity_name}",
                        "runId": run_id,
                        "sequence": 1,
                        "cursor": f"{run_id}:1",
                        "releaseId": "release-authorization-matrix",
                        "occurredAt": f"2026-08-08T00:00:0{index}Z",
                    },
                    "payload": {},
                },
            )
            if resource_state == "accepted":
                app._pending_accepted_runs_by_run_id[run_id] = {
                    "responseId": f"response-{identity_name}",
                    "releaseId": "release-authorization-matrix",
                    "policySnapshotId": "policy-authorization-matrix",
                    "turnId": None,
                }

        for identity_name, _principal in _IDENTITIES:
            response = app.handle(
                ServerRequest(
                    method="GET",
                    path="/runs",
                    headers={"authorization": f"Bearer token-{identity_name}"},
                    query={},
                    cookies={},
                )
            )

            case = f"{resource_state}:{identity_name}"
            assert response.status_code == 200, case
            payload = json.loads(response.body)
            assert [run["runId"] for run in payload["runs"]] == [
                expected_run_by_identity[identity_name]
            ], case
