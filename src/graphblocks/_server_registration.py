"""Server callback registration and delivery-control operation handlers.

Extracted from ``graphblocks.server``: the ``callback_registration`` and
``callback_delivery_control`` dispatch-table entries now point at the free
functions below, whose bodies are the former ``GraphBlocksServerApp``
methods moved verbatim with ``self`` renamed to ``app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from typing import TYPE_CHECKING

from ._server_auth import ServerAuthDecision
from ._server_callbacks import (
    _server_idempotency_key,
    ServerCallbackDeliveryResult,
    ServerCallbackRegistration,
)
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_seams import _server_request_json_body
from ._server_validation import (
    _constrain_event_filter_visibility,
    _principal_matches_owner,
    _utc_now_iso,
    _validate_exact_non_empty_string,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


def _handle_callback_registration_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "register_callback":
        reserved_callback_registration_bytes = 0
        reserved_callback_registration_id: str | None = None
        callback_registration_capacity_committed = False
        try:
            with app._callback_registration_condition:
                registration = ServerCallbackRegistration.from_request(
                    request=request,
                    ordinal=len(
                        set(app._callback_registrations).union(
                            app._pending_callback_registration_ids
                        )
                    )
                    + 1,
                    owner=auth_decision.principal,
                )
                registration = replace(
                    registration,
                    event_filter=_constrain_event_filter_visibility(
                        registration.event_filter,
                        auth_decision.principal,
                    ),
                )
                if (
                    auth_decision.principal is not None
                    and auth_decision.principal.tenant_id is not None
                    and registration.scope == "tenant"
                    and registration.scope_id != auth_decision.principal.tenant_id
                ):
                    return ServerResponse.json(
                        403,
                        {
                            "ok": False,
                            "error": (
                                f"callback registration tenant scope {registration.scope_id!r} "
                                "is not allowed for principal tenant "
                                f"{auth_decision.principal.tenant_id!r}"
                            ),
                        },
                    )
                if (
                    registration.scope == "run"
                    and not app._principal_can_access_run(
                        registration.scope_id,
                        auth_decision.principal,
                    )
                ):
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": (
                                "run event stream not found for callback "
                                "registration scope "
                                f"{registration.scope_id!r}"
                            ),
                        },
                    )
                if registration.subscription_id in app._pending_callback_registration_ids:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "subscriptionId": registration.subscription_id,
                            "state": "pending",
                            "error": f"callback registration {registration.subscription_id!r} already exists",
                        },
                    )
                existing = app._callback_registrations.get(registration.subscription_id)
                resuming_incomplete_registration = False
                if existing is not None:
                    retry_registration = replace(registration, created_at=existing.created_at)
                    if (
                        registration.subscription_id
                        not in app._incomplete_callback_registration_ids
                        or retry_registration != existing
                    ):
                        return ServerResponse.json(
                            409,
                            {
                                "ok": False,
                                "subscriptionId": registration.subscription_id,
                                "state": existing.status,
                                "error": (
                                    f"callback registration {registration.subscription_id!r} already exists"
                                ),
                            },
                        )
                    registration = existing
                    resuming_incomplete_registration = True
                if not resuming_incomplete_registration:
                    retained_registration_bytes = (
                        registration._retained_size_bytes()
                    )
                    pending_new_registration_count = sum(
                        pending_subscription_id
                        not in app._callback_registrations
                        for pending_subscription_id in (
                            app._pending_callback_registration_ids
                        )
                    )
                    if (
                        len(app._callback_registrations)
                        + pending_new_registration_count
                        >= app.max_callback_registrations
                        or (
                            app._callback_registration_history_bytes
                            + retained_registration_bytes
                            > app.max_callback_registration_history_bytes
                        )
                    ):
                        return ServerResponse.json(
                            429,
                            {
                                "ok": False,
                                "subscriptionId": registration.subscription_id,
                                "reasonCode": (
                                    "server.callback_registration_"
                                    "capacity_exhausted"
                                ),
                                "maxCallbackRegistrations": (
                                    app.max_callback_registrations
                                ),
                                "maxCallbackRegistrationHistoryBytes": (
                                    app.max_callback_registration_history_bytes
                                ),
                                "error": (
                                    "server callback registration history "
                                    "capacity is exhausted"
                                ),
                            },
                        )
                    app._callback_registration_history_bytes += (
                        retained_registration_bytes
                    )
                    reserved_callback_registration_bytes = (
                        retained_registration_bytes
                    )
                    reserved_callback_registration_id = (
                        registration.subscription_id
                    )
                app._pending_callback_registration_ids.add(registration.subscription_id)

            replay_ready = False
            try:
                replay = app._callback_registration_replay(registration)
                if isinstance(replay, ServerResponse):
                    return replay
                replayed_events, last_cursor = replay
                replay_ready = True
            finally:
                if not replay_ready:
                    with app._callback_registration_condition:
                        app._pending_callback_registration_ids.discard(registration.subscription_id)

            with app._callback_registration_condition:
                if (
                    registration.scope == "run"
                    and (
                        registration.scope_id
                        not in app._events_by_run_id
                        or not app._principal_can_access_run(
                            registration.scope_id,
                            auth_decision.principal,
                        )
                    )
                ):
                    app._pending_callback_registration_ids.discard(
                        registration.subscription_id
                    )
                    app._incomplete_callback_registration_ids.discard(
                        registration.subscription_id
                    )
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": (
                                "run event stream not found for callback "
                                "registration scope "
                                f"{registration.scope_id!r}"
                            ),
                        },
                    )
                if not resuming_incomplete_registration:
                    app._callback_registrations[registration.subscription_id] = registration
                    callback_registration_capacity_committed = True
                if (
                    app.callback_delivery_hook is not None
                    and registration.delivery.get("kind") == "webhook"
                ):
                    app._incomplete_callback_registration_ids.add(
                        registration.subscription_id
                    )
                    app._active_callback_delivery_registration_ids.add(
                        registration.subscription_id
                    )

            delivery_results: tuple[ServerCallbackDeliveryResult, ...] = ()
            if app.callback_delivery_hook is not None and registration.delivery.get("kind") == "webhook":
                with app._callback_registration_condition:
                    delivered = list(
                        app._callback_delivery_results_by_subscription_id.get(
                            registration.subscription_id,
                            (),
                        )
                    )
                completed_event_ids = {result.event_id for result in delivered}
                try:
                    for event in replayed_events:
                        metadata = event.get("metadata")
                        event_id = metadata.get("eventId") if isinstance(metadata, Mapping) else None
                        if isinstance(event_id, str) and event_id in completed_event_ids:
                            continue
                        try:
                            delivery_result = app.callback_delivery_hook.deliver(registration, event)
                        except Exception:
                            return ServerResponse.json(
                                502,
                                {
                                    "ok": False,
                                    "subscriptionId": registration.subscription_id,
                                    "error": "callback delivery hook failed closed",
                                },
                            )
                        if not isinstance(delivery_result, ServerCallbackDeliveryResult):
                            return ServerResponse.json(
                                502,
                                {
                                    "ok": False,
                                    "subscriptionId": registration.subscription_id,
                                    "error": "callback delivery hook returned an invalid result",
                                },
                            )
                        if delivery_result.subscription_id != registration.subscription_id:
                            return ServerResponse.json(
                                502,
                                {
                                    "ok": False,
                                    "subscriptionId": registration.subscription_id,
                                    "error": "callback delivery hook returned a mismatched subscription",
                                },
                            )
                        if isinstance(event_id, str) and delivery_result.event_id != event_id:
                            return ServerResponse.json(
                                502,
                                {
                                    "ok": False,
                                    "subscriptionId": registration.subscription_id,
                                    "error": "callback delivery hook returned a mismatched event",
                                },
                            )
                        if delivery_result.state_version != 1:
                            return ServerResponse.json(
                                502,
                                {
                                    "ok": False,
                                    "subscriptionId": registration.subscription_id,
                                    "error": (
                                        "callback delivery hook returned a non-initial "
                                        "state version"
                                    ),
                                },
                            )
                        with app._callback_registration_condition:
                            current_deliveries = app._callback_delivery_results_by_subscription_id.get(
                                registration.subscription_id
                            )
                            if type(current_deliveries) is not list:
                                current_deliveries = list(
                                    current_deliveries or ()
                                )
                                app._callback_delivery_results_by_subscription_id[
                                    registration.subscription_id
                                ] = current_deliveries
                            current_event_result = next(
                                (
                                    current
                                    for current in current_deliveries
                                    if current.event_id == delivery_result.event_id
                                ),
                                None,
                            )
                            if current_event_result is not None:
                                if (
                                    current_event_result.delivery_id
                                    != delivery_result.delivery_id
                                    or current_event_result.run_id != delivery_result.run_id
                                    or current_event_result.sequence
                                    != delivery_result.sequence
                                    or current_event_result.cursor != delivery_result.cursor
                                    or current_event_result.idempotency_key
                                    != delivery_result.idempotency_key
                                ):
                                    return ServerResponse.json(
                                        502,
                                        {
                                            "ok": False,
                                            "subscriptionId": (
                                                registration.subscription_id
                                            ),
                                            "error": (
                                                "callback delivery hook completion "
                                                "conflicts with retained event identity"
                                            ),
                                        },
                                    )
                                if (
                                    current_event_result != delivery_result
                                    and current_event_result.state_version
                                    <= delivery_result.state_version
                                ):
                                    return ServerResponse.json(
                                        502,
                                        {
                                            "ok": False,
                                            "subscriptionId": (
                                                registration.subscription_id
                                            ),
                                            "error": (
                                                "callback delivery hook completion "
                                                "conflicts with retained event state"
                                            ),
                                        },
                                    )
                            elif any(
                                current.delivery_id == delivery_result.delivery_id
                                or current.idempotency_key
                                == delivery_result.idempotency_key
                                for current in current_deliveries
                            ):
                                return ServerResponse.json(
                                    502,
                                    {
                                        "ok": False,
                                        "subscriptionId": registration.subscription_id,
                                        "error": (
                                            "callback delivery hook completion reuses a "
                                            "retained delivery identity"
                                        ),
                                    },
                                )
                            else:
                                current_deliveries.append(delivery_result)
                        completed_event_ids.add(delivery_result.event_id)
                    with app._callback_registration_condition:
                        delivery_results = tuple(
                            app._callback_delivery_results_by_subscription_id.get(
                                registration.subscription_id,
                                (),
                            )
                        )
                        app._incomplete_callback_registration_ids.discard(
                            registration.subscription_id
                        )
                finally:
                    with app._callback_registration_condition:
                        app._pending_callback_registration_ids.discard(registration.subscription_id)
                        app._active_callback_delivery_registration_ids.discard(
                            registration.subscription_id
                        )
            else:
                with app._callback_registration_condition:
                    app._pending_callback_registration_ids.discard(registration.subscription_id)
            return ServerResponse.json(
                200 if resuming_incomplete_registration else 201,
                registration.response_payload(replayed_events, last_cursor, delivery_results),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.callback_registration.invalid_request",
                message="The callback registration request is invalid.",
                error=error,
            )
        finally:
            if (
                reserved_callback_registration_bytes
                and not callback_registration_capacity_committed
            ):
                with app._callback_registration_condition:
                    app._callback_registration_history_bytes -= (
                        reserved_callback_registration_bytes
                    )
                    if reserved_callback_registration_id is not None:
                        app._pending_callback_registration_ids.discard(
                            reserved_callback_registration_id
                        )
    if route.operation == "revoke_callback":
        subscription_id = route_match.path_params.get("subscription_id", "")
        with app._callback_registration_condition:
            registration = app._callback_registrations.get(subscription_id)
            if registration is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"callback registration {subscription_id!r} not found",
                    },
                )
            if registration.owner is not None and not _principal_matches_owner(
                auth_decision.principal,
                registration.owner,
            ):
                return ServerResponse.json(
                    403,
                    {
                        "ok": False,
                        "error": f"callback registration {subscription_id!r} belongs to a different principal",
                    },
                )
            if registration.status == "revoked":
                return ServerResponse.json(
                    200,
                    {
                        "ok": True,
                        "subscriptionId": subscription_id,
                        "status": "revoked",
                        "duplicate": True,
                    },
                )
            revoked = replace(registration, status="revoked")
            app._callback_registrations[subscription_id] = revoked
            return ServerResponse.json(
                202,
                {
                    "ok": True,
                    "subscriptionId": subscription_id,
                    "status": "revoked",
                },
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


def _handle_callback_delivery_control_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation in {"redrive_callback_delivery", "move_callback_to_dead_letter"}:
        try:
            delivery_id = route_match.path_params.get("delivery_id", "")
            payload = _server_request_json_body(request, "callback delivery control request")
            if not isinstance(payload, Mapping):
                raise ValueError("callback delivery control request body must be a JSON object")
            idempotency_value = _server_idempotency_key(
                payload,
                request.headers,
                owner="callback delivery control request",
            )
            idempotency_key = (
                None
                if idempotency_value == ""
                else _validate_exact_non_empty_string(
                    "callback delivery control request",
                    "idempotency_key",
                    idempotency_value,
                )
            )
            return app._callback_delivery_control_response(
                delivery_id,
                route.operation,
                payload,
                request.requested_at or _utc_now_iso(),
                auth_decision.principal,
                idempotency_key=idempotency_key,
            )
        except PermissionError as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=403,
                error_code="server.callback_delivery.forbidden",
                message="The callback delivery operation is not permitted.",
                error=error,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.callback_delivery.invalid_request",
                message="The callback delivery request is invalid.",
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
