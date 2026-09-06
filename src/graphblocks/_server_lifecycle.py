"""Server system and run-lifecycle operation handlers.

Extracted from ``graphblocks.server``: the ``system`` and ``run_lifecycle``
dispatch-table entries now point at the free functions below, whose bodies
are the former ``GraphBlocksServerApp`` methods moved verbatim with
``self`` renamed to ``app``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
import json
from typing import TYPE_CHECKING

from ._server_auth import (
    _ServerRunDeletionConflictError,
    ServerAuthDecision,
)
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_seams import _server_request_json_body
from ._server_validation import (
    _decode_run_list_cursor,
    _encode_run_list_cursor,
    _run_list_scope,
    _utc_now_iso,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


def _handle_system_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    del request, route_match, auth_decision
    payload = app.health.to_payload()
    checks = payload.get("checks")
    if app._audit_hook_failures_by_hook and isinstance(checks, dict):
        checks["audit_hooks"] = {
            "status": "degraded",
            "details": {
                "hook_failure_counts": dict(
                    sorted(app._audit_hook_failures_by_hook.items())
                ),
            },
        }
        if payload["status"] == "healthy":
            payload["status"] = "degraded"
    return ServerResponse.json(200, payload)


def _handle_run_lifecycle_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "list_runs":
        try:
            scope = _run_list_scope(auth_decision.principal)
            cursor = request.query.get("cursor")
            after_run_id = (
                _decode_run_list_cursor(cursor, scope)
                if cursor is not None
                else None
            )
            page_limit = app.max_run_list_page_runs
            requested_limit = request.query.get("limit")
            if requested_limit is not None:
                if requested_limit != requested_limit.strip():
                    raise ValueError(
                        "server run list limit must not contain "
                        "surrounding whitespace"
                    )
                if (
                    not requested_limit.isascii()
                    or not requested_limit.isdecimal()
                ):
                    raise ValueError(
                        "server run list limit must be a positive "
                        "ASCII integer"
                    )
                if len(requested_limit) > len(
                    str(app.max_run_list_page_runs)
                ):
                    raise ValueError(
                        "server run list limit must be at most "
                        f"{app.max_run_list_page_runs}"
                    )
                page_limit = int(requested_limit)
                if page_limit < 1:
                    raise ValueError(
                        "server run list limit must be a positive "
                        "ASCII integer"
                    )
                if page_limit > app.max_run_list_page_runs:
                    raise ValueError(
                        "server run list limit must be at most "
                        f"{app.max_run_list_page_runs}"
                    )
            scope_kind, tenant_id, owner_principal_id = scope
            with app._accepted_run_condition:
                if scope_kind == "tenant":
                    indexed_run_ids = app._run_ids_by_tenant.get(
                        tenant_id,
                        (),
                    )
                else:
                    indexed_run_ids = app._run_ids_by_owner.get(
                        (tenant_id, owner_principal_id),
                        (),
                    )
                page_start = (
                    bisect_right(indexed_run_ids, after_run_id)
                    if after_run_id is not None
                    else 0
                )
                page_run_ids = tuple(
                    indexed_run_ids[
                        page_start : page_start + page_limit + 1
                    ]
                )
                has_more = len(page_run_ids) > page_limit
                page_run_ids = page_run_ids[:page_limit]
                page_records = tuple(
                    (run_id, app._events_by_run_id[run_id])
                    for run_id in page_run_ids
                    if (
                        run_id in app._events_by_run_id
                        and app._principal_can_access_run(
                            run_id,
                            auth_decision.principal,
                        )
                    )
                )
            runs = [
                app._run_status_payload(
                    run_id,
                    events,
                    include_ok=False,
                )
                for run_id, events in page_records
            ]
            next_cursor = (
                _encode_run_list_cursor(scope, page_run_ids[-1])
                if has_more and page_run_ids
                else None
            )
        except (TypeError, ValueError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run_list.invalid_request",
                message="The run-list request is invalid.",
                error=error,
            )
        return ServerResponse.json(
            200,
            {
                "ok": True,
                "runs": runs,
                "nextCursor": next_cursor,
            },
        )
    if route.operation == "delete_run":
        run_id = route_match.path_params.get("run_id", "")
        try:
            payload = app.delete_terminal_run(
                run_id,
                principal=auth_decision.principal,
                deleted_at=request.requested_at or _utc_now_iso(),
            )
        except KeyError:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run {run_id!r} was not found",
                },
            )
        except _ServerRunDeletionConflictError as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=409,
                error_code="server.run.delete_conflict",
                message="The run cannot be deleted in its current state.",
                error=error,
            )
        except ValueError as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run_delete.invalid_request",
                message="The run deletion request is invalid.",
                error=error,
            )
        return ServerResponse.json(
            200 if payload["duplicate"] else 202,
            payload,
        )
    if route.operation in {"cancel_run", "pause_run", "resume_run", "expire_run"}:
        try:
            run_id = route_match.path_params.get("run_id", "")
            with app._accepted_run_condition:
                events = app._events_by_run_id.get(run_id)
                if events is None or not app._principal_can_access_run(
                    run_id,
                    auth_decision.principal,
                ):
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run control stream not found for run {run_id!r}",
                        },
                    )
            payload = _server_request_json_body(request, "run control request")
            if not isinstance(payload, Mapping):
                raise ValueError("run control request body must be a JSON object")
            with app._accepted_run_condition:
                events = app._events_by_run_id.get(run_id)
                if events is None or not app._principal_can_access_run(
                    run_id,
                    auth_decision.principal,
                ):
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run control stream not found for run {run_id!r}",
                        },
                    )
                return app._run_control_response(
                    run_id,
                    route.operation,
                    events,
                    payload,
                    request.requested_at or _utc_now_iso(),
                    auth_decision.principal,
                )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run_control.invalid_request",
                message="The run control request is invalid.",
                error=error,
            )
    if route.operation == "get_run_status":
        run_id = route_match.path_params.get("run_id", "")
        if not app._principal_can_access_run(run_id, auth_decision.principal):
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run status not found for run {run_id!r}",
                },
            )
        events = app._events_by_run_id.get(run_id)
        if events is None:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run status not found for run {run_id!r}",
                },
            )
        try:
            return ServerResponse.json(200, app._run_status_payload(run_id, events))
        except (TypeError, ValueError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=500,
                error_code="server.run_status.projection_failed",
                message="The run status is temporarily unavailable.",
                error=error,
            )
    if route.operation == "attach_to_run":
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
                        "error": f"run attach stream not found for run {run_id!r}",
                    },
                )
            events = app._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run attach stream not found for run {run_id!r}",
                    },
                )
            payload = _server_request_json_body(request, "attach request")
            if not isinstance(payload, Mapping):
                raise ValueError("attach request body must be a JSON object")
            return app._attach_to_run_response(run_id, events, payload, auth_decision.principal)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run_attach.invalid_request",
                message="The run attach request is invalid.",
                error=error,
            )
    if route.operation == "detach_from_run":
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
                        "error": f"run detach stream not found for run {run_id!r}",
                    },
                )
            events = app._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run detach stream not found for run {run_id!r}",
                    },
                )
            payload = _server_request_json_body(request, "detach request")
            if not isinstance(payload, Mapping):
                raise ValueError("detach request body must be a JSON object")
            with app._accepted_run_condition:
                events = app._events_by_run_id.get(run_id)
                if (
                    events is None
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
                                "run detach stream not found for run "
                                f"{run_id!r}"
                            ),
                        },
                    )
                return app._detach_from_run_response(
                    run_id,
                    events,
                    payload,
                    request.requested_at or _utc_now_iso(),
                )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run_detach.invalid_request",
                message="The run detach request is invalid.",
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
