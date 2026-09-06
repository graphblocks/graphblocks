"""Server event-subscription operation handler.

Extracted from ``graphblocks.server``: the ``subscriptions`` dispatch-table
entry now points at the free function below, whose body is the former
``GraphBlocksServerApp`` method moved verbatim with ``self`` renamed to
``app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from typing import TYPE_CHECKING

from ._server_auth import ServerAuthDecision
from ._server_callbacks import ServerEventSubscription
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_seams import (
    _canonical_json_size_bytes,
    _server_request_json_body,
)
from ._server_validation import (
    _constrain_event_filter_visibility,
    _principal_matches_owner,
    _utc_now_iso,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


def _handle_subscription_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "subscribe_events":
        try:
            run_id = route_match.path_params.get("run_id", "")
            if not app._principal_can_access_run(
                run_id,
                auth_decision.principal,
            ):
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": (
                            "run event stream not found for subscription "
                            f"run {run_id!r}"
                        ),
                    },
                )
            events = app._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run event stream not found for subscription run {run_id!r}",
                    },
                )
            with app._subscription_registration_condition:
                current_events = app._events_by_run_id.get(run_id)
                if (
                    current_events is None
                    or not app._principal_can_access_run(
                        run_id,
                        auth_decision.principal,
                    )
                ):
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": (
                                "run event stream not found for subscription "
                                f"run {run_id!r}"
                            ),
                        },
                    )
                events = current_events
                existing = app._subscriptions_by_run_id.get(run_id, ())
                subscription = ServerEventSubscription.from_request(
                    run_id=run_id,
                    request=request,
                    ordinal=len(existing) + 1,
                    owner=auth_decision.principal,
                )
                subscription = replace(
                    subscription,
                    event_filter=_constrain_event_filter_visibility(
                        subscription.event_filter,
                        auth_decision.principal,
                    ),
                )
                existing_subscription = app._subscription_for(run_id, subscription.subscription_id)
                if existing_subscription is not None:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "runId": run_id,
                            "subscriptionId": subscription.subscription_id,
                            "state": existing_subscription.status,
                            "error": (
                                f"subscription {subscription.subscription_id!r} "
                                f"already exists for run {run_id!r}"
                            ),
                        },
                    )
                retained_subscription_bytes = (
                    app._subscription_history_bytes_by_run_id.get(
                        run_id
                    )
                )
                if retained_subscription_bytes is None:
                    retained_subscription_bytes = 0
                    for retained_subscription in existing:
                        retained_value = (
                            retained_subscription.response_payload(
                                [],
                                f"{run_id}:0",
                            )
                        )
                        retained_value["createdAt"] = (
                            retained_subscription.created_at
                        )
                        retained_value["status"] = "revoked"
                        retained_subscription_bytes += (
                            _canonical_json_size_bytes(retained_value)
                        )
                    app._subscription_history_bytes_by_run_id[
                        run_id
                    ] = retained_subscription_bytes
                candidate_value = subscription.response_payload(
                    [],
                    f"{run_id}:0",
                )
                candidate_value["createdAt"] = subscription.created_at
                candidate_value["status"] = "revoked"
                candidate_bytes = _canonical_json_size_bytes(
                    candidate_value
                )
                if (
                    len(existing) >= app.max_subscriptions_per_run
                    or (
                        retained_subscription_bytes
                        + candidate_bytes
                        > app.max_subscription_history_bytes_per_run
                    )
                ):
                    return ServerResponse.json(
                        429,
                        {
                            "ok": False,
                            "runId": run_id,
                            "reasonCode": (
                                "server.run_subscription_capacity_exhausted"
                            ),
                            "maxSubscriptionsPerRun": (
                                app.max_subscriptions_per_run
                            ),
                            "maxSubscriptionHistoryBytesPerRun": (
                                app.max_subscription_history_bytes_per_run
                            ),
                            "error": (
                                "server run subscription history capacity "
                                "is exhausted"
                            ),
                        },
                    )
                replay = app._subscription_replay(subscription, events)
                if isinstance(replay, ServerResponse):
                    return replay
                app._subscriptions_by_run_id.setdefault(
                    run_id,
                    [],
                ).append(subscription)
                app._subscription_history_bytes_by_run_id[run_id] = (
                    retained_subscription_bytes + candidate_bytes
                )
            return ServerResponse.json(
                201,
                subscription.response_payload(replay, f"{run_id}:{app._last_event_sequence(events)}"),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.subscription.invalid_request",
                message="The subscription request is invalid.",
                error=error,
            )
    if route.operation == "unsubscribe_events":
        run_id = route_match.path_params.get("run_id", "")
        subscription_id = route_match.path_params.get("subscription_id", "")
        if not app._principal_can_access_run(
            run_id,
            auth_decision.principal,
        ):
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run subscriptions not found for run {run_id!r}",
                },
            )
        with app._subscription_registration_condition:
            subscriptions = app._subscriptions_by_run_id.get(run_id)
            if subscriptions is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run subscriptions not found for run {run_id!r}",
                    },
                )
            for index, subscription in enumerate(subscriptions):
                if subscription.subscription_id == subscription_id:
                    if subscription.owner is not None and not _principal_matches_owner(
                        auth_decision.principal,
                        subscription.owner,
                    ):
                        return ServerResponse.json(
                            403,
                            {
                                "ok": False,
                                "error": (
                                    f"subscription {subscription_id!r} for run {run_id!r} "
                                    "belongs to a different principal"
                                ),
                            },
                        )
                    if subscription.status == "revoked":
                        return ServerResponse.json(
                            200,
                            {
                                "ok": True,
                                "runId": run_id,
                                "subscriptionId": subscription_id,
                                "status": "revoked",
                                "duplicate": True,
                            },
                        )
                    revoked = replace(subscription, status="revoked")
                    subscriptions[index] = revoked
                    return ServerResponse.json(
                        202,
                        {
                            "ok": True,
                            "runId": run_id,
                            "subscriptionId": subscription_id,
                            "status": "revoked",
                        },
                    )
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"subscription {subscription_id!r} not found for run {run_id!r}",
                },
            )
    if route.operation == "ack_event":
        try:
            run_id = route_match.path_params.get("run_id", "")
            subscription_id = route_match.path_params.get("subscription_id", "")
            if not app._principal_can_access_run(
                run_id,
                auth_decision.principal,
            ):
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": (
                            "run event stream not found for ack "
                            f"run {run_id!r}"
                        ),
                    },
                )
            events = app._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run event stream not found for ack run {run_id!r}",
                    },
                )
            with app._subscription_registration_condition:
                subscription = app._subscription_for(run_id, subscription_id)
                if subscription is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"subscription {subscription_id!r} not found for run {run_id!r}",
                        },
                    )
                if subscription.owner is not None and not _principal_matches_owner(
                    auth_decision.principal,
                    subscription.owner,
                ):
                    return ServerResponse.json(
                        403,
                        {
                            "ok": False,
                            "error": (
                                f"subscription {subscription_id!r} for run {run_id!r} "
                                "belongs to a different principal"
                            ),
                        },
                    )
                if subscription.status != "active":
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "runId": run_id,
                            "subscriptionId": subscription_id,
                            "state": subscription.status,
                            "error": (
                                f"subscription {subscription_id!r} for run {run_id!r} "
                                f"is {subscription.status}"
                            ),
                        },
                    )
                payload = _server_request_json_body(request, "ack request")
                if not isinstance(payload, Mapping):
                    raise ValueError("ack request body must be a JSON object")
                return app._ack_event_response(
                    run_id,
                    subscription_id,
                    subscription,
                    events,
                    payload,
                    request.requested_at or _utc_now_iso(),
                )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.subscription_ack.invalid_request",
                message="The subscription acknowledgement request is invalid.",
                error=error,
            )
    return ServerResponse.json(
        501,
        {
            "ok": False,
            "error": (
                f"server operation {route.operation!r} is not implemented"
            ),
        },
    )
