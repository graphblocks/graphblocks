"""Server execution (run admission) operation handler.

Extracted from ``graphblocks.server``: the ``execution`` dispatch-table
entry now points at the free function below, whose body is the former
``GraphBlocksServerApp`` method moved verbatim with ``self`` renamed to
``app``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
from threading import get_ident
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

from ._server_auth import (
    _AcceptedRunAdmissionReservation,
    _AcceptedRunExecution,
    _ServerRunAuthorizationRecord,
    ServerAuthDecision,
)
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_validation import (
    _DURABLE_ACCEPTED_RUN_REQUIRED_ERROR,
    _DURABLE_ACCEPTED_RUN_REQUIRED_REASON_CODE,
    _freeze_json_value,
    _server_alias_value,
    _server_request_json_body,
    _utc_now_iso,
    _validate_exact_non_empty_string,
    _validate_iso_datetime,
)
from .admission import (
    AdmissionIdempotencyConflictError,
    AdmissionQueueFullError,
    AdmissionTicket,
    AdmissionTicketNotFoundError,
)
from .application_event import (
    ApplicationEvent,
    ApplicationEventMetadata,
)
from .runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


_compile_graph_seam: Callable[..., object] | None = None


def _install_compile_graph_seam(seam: Callable[..., object]) -> None:
    """Install the call-time compile resolver.

    ``graphblocks.server`` installs a closure over its own ``compile_graph``
    module attribute when it imports this handler. Tests monkeypatch
    ``compile_graph`` on the ``graphblocks.server`` module to stub run
    compilation; resolving through the installed seam on every call keeps
    that working now that the execution handler lives here, without this
    module ever importing ``graphblocks.server`` (the internal import graph
    must stay acyclic).
    """
    global _compile_graph_seam
    _compile_graph_seam = seam


def compile_graph(*args: object, **kwargs: object) -> object:
    seam = _compile_graph_seam
    if seam is None:
        from .compiler import compile_graph as direct_compile_graph

        return direct_compile_graph(*args, **kwargs)
    return seam(*args, **kwargs)


def _handle_execution_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "invoke_graph":
        accepted_run_reservation: (
            _AcceptedRunAdmissionReservation | None
        ) = None
        try:
            payload = _server_request_json_body(request, "run request")
            if not isinstance(payload, dict):
                raise ValueError("run request body must be a JSON object")
            graph = payload.get("graph")
            if not isinstance(graph, dict):
                raise ValueError("run request body requires graph object")
            inputs = payload.get("inputs", {})
            if not isinstance(inputs, dict):
                raise ValueError("run request inputs must be a JSON object")
            response_mode = _validate_exact_non_empty_string(
                "run request",
                "responseMode",
                _server_alias_value(
                    payload,
                    "run request",
                    "response_mode",
                    "responseMode",
                    "sync",
                ),
            )
            if response_mode not in {"sync", "accepted", "background"}:
                raise ValueError("run request responseMode must be one of sync, accepted, or background")
            run_id_value = _server_alias_value(
                payload,
                "run request",
                "run_id",
                "runId",
            )
            if "run_id" not in payload and "runId" not in payload:
                run_id_value = f"run-{uuid4().hex}"
            run_id = _validate_exact_non_empty_string(
                "run request",
                "runId",
                run_id_value,
            )
            request_id = _validate_exact_non_empty_string(
                "run request",
                "requestId",
                _server_alias_value(
                    payload,
                    "run request",
                    "request_id",
                    "requestId",
                    run_id,
                ),
            )
            admission_owner_id = (
                auth_decision.principal.principal_id
                if auth_decision.principal is not None
                else "anonymous"
            )
            run_tenant_id = (
                auth_decision.principal.tenant_id
                if auth_decision.principal is not None
                else None
            )
            if (
                response_mode in {"accepted", "background"}
                and not app.allow_process_local_accepted_runs_dev
            ):
                return ServerResponse.json(
                    503,
                    {
                        "ok": False,
                        "reasonCode": (
                            _DURABLE_ACCEPTED_RUN_REQUIRED_REASON_CODE
                        ),
                        "error": _DURABLE_ACCEPTED_RUN_REQUIRED_ERROR,
                    },
                )
            if run_id in app._events_by_run_id:
                if not app._principal_can_access_run(
                    run_id,
                    auth_decision.principal,
                ):
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run {run_id!r} was not found",
                        },
                    )
                existing_ticket_id = app._admission_ticket_ids_by_run_id.get(
                    run_id
                )
                if (
                    existing_ticket_id is not None
                    and app.admission_ticket_queue is not None
                    and response_mode in {"accepted", "background"}
                ):
                    try:
                        existing_ticket = app.admission_ticket_queue.get(
                            existing_ticket_id
                        )
                    except AdmissionTicketNotFoundError:
                        # Pruned terminal ticket: treat the resubmission
                        # as a conflict with the retained run below.
                        existing_ticket = None
                    if (
                        existing_ticket is not None
                        and existing_ticket.request_id == request_id
                        and existing_ticket.owner_id == admission_owner_id
                        and existing_ticket.tenant_id == run_tenant_id
                    ):
                        route_run_id = quote(run_id, safe="")
                        return ServerResponse.json(
                            202,
                            {
                                "ok": True,
                                "runId": run_id,
                                "status": response_mode,
                                "eventStream": f"/runs/{route_run_id}/events",
                                "websocket": f"/runs/{route_run_id}/ws",
                                "cancel": f"/runs/{route_run_id}/cancel",
                                "initialCursor": f"{run_id}:0",
                                "admissionTicket": existing_ticket.contract(),
                                "duplicate": True,
                            },
                        )
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "error": f"run {run_id!r} already exists",
                    },
                )
            response_id_value = _server_alias_value(
                payload,
                "run request",
                "response_id",
                "responseId",
            )
            if "response_id" not in payload and "responseId" not in payload:
                response_id_value = f"response-{uuid4().hex}"
            response_id = _validate_exact_non_empty_string(
                "run request",
                "responseId",
                response_id_value,
            )
            release_id = _validate_exact_non_empty_string(
                "run request",
                "releaseId",
                _server_alias_value(
                    payload,
                    "run request",
                    "release_id",
                    "releaseId",
                    "local",
                ),
            )
            policy_snapshot_id = _validate_exact_non_empty_string(
                "run request",
                "policySnapshotId",
                _server_alias_value(
                    payload,
                    "run request",
                    "policy_snapshot_id",
                    "policySnapshotId",
                    "local",
                ),
            )
            occurred_at = _server_alias_value(
                payload,
                "run request",
                "occurred_at",
                "occurredAt",
            )
            if occurred_at is None:
                occurred_at = _utc_now_iso()
            occurred_at = _validate_iso_datetime("run request", "occurredAt", occurred_at)
            turn_id_value = _server_alias_value(
                payload,
                "run request",
                "turn_id",
                "turnId",
            )
            turn_id = (
                _validate_exact_non_empty_string("run request", "turnId", turn_id_value)
                if turn_id_value is not None
                else None
            )
            ticketed_admission = (
                app.admission_ticket_queue is not None
                and response_mode in {"accepted", "background"}
            )
            admission_units = _server_alias_value(
                payload,
                "run request",
                "admission_units",
                "admissionUnits",
                1,
            )
            if (
                not isinstance(admission_units, int)
                or isinstance(admission_units, bool)
                or admission_units < 1
            ):
                raise ValueError("run request admissionUnits must be a positive integer")
            reservation_candidate = _AcceptedRunAdmissionReservation(
                authorization=_ServerRunAuthorizationRecord.create(
                    run_id,
                    auth_decision.principal,
                    occurred_at,
                ),
                reservation_id=f"admission-{uuid4().hex}",
            )
            terminal_collection_now = _validate_iso_datetime(
                "server terminal run collection clock",
                "now",
                app.terminal_run_collection_clock(),
            )
            terminal_collection_datetime = datetime.fromisoformat(
                (
                    f"{terminal_collection_now[:-1]}+00:00"
                    if terminal_collection_now.endswith("Z")
                    else terminal_collection_now
                )
            ).astimezone(timezone.utc)
            collect_terminal_runs = False
            with app._accepted_run_condition:
                while app._terminal_run_collection_in_progress:
                    app._accepted_run_condition.wait()
                if (
                    app._last_terminal_run_collection_at is None
                    or terminal_collection_datetime
                    - app._last_terminal_run_collection_at
                    >= timedelta(
                        seconds=(
                            app.terminal_run_collection_interval_seconds
                        )
                    )
                ):
                    app._terminal_run_collection_in_progress = True
                    collect_terminal_runs = True
            if collect_terminal_runs:
                try:
                    app.collect_expired_terminal_runs(
                        now=terminal_collection_now
                    )
                finally:
                    with app._accepted_run_condition:
                        app._last_terminal_run_collection_at = (
                            terminal_collection_datetime
                        )
                        app._terminal_run_collection_in_progress = False
                        app._accepted_run_condition.notify_all()

            with app._accepted_run_condition:
                active_reservation = (
                    app._accepted_run_reservations_by_run_id.get(
                        run_id
                    )
                )
                active_authorization = (
                    app._run_authorization_by_run_id.get(run_id)
                    if run_id in app._events_by_run_id
                    else (
                        active_reservation.authorization
                        if active_reservation is not None
                        else (
                            app._retired_runs_by_run_id[
                                run_id
                            ].authorization
                            if run_id in app._retired_runs_by_run_id
                            else None
                        )
                    )
                )
                if (
                    run_id in app._events_by_run_id
                    or active_reservation is not None
                    or run_id in app._retired_runs_by_run_id
                ):
                    if (
                        active_authorization is None
                        or not active_authorization.allows_read(
                            auth_decision.principal
                        )
                    ):
                        return ServerResponse.json(
                            404,
                            {
                                "ok": False,
                                "error": (
                                    f"run {run_id!r} was not found"
                                ),
                            },
                        )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "runId": run_id,
                            "error": f"run {run_id!r} already exists",
                        },
                    )
                if (
                    len(app._events_by_run_id)
                    + len(
                        app._accepted_run_reservations_by_run_id
                    )
                    >= app.max_in_memory_runs
                ):
                    return ServerResponse.json(
                        429,
                        {
                            "ok": False,
                            "runId": run_id,
                            "reasonCode": "server.run_capacity_exhausted",
                            "maxInMemoryRuns": app.max_in_memory_runs,
                            "error": (
                                "server in-memory run capacity is exhausted"
                            ),
                        },
                    )
                in_memory_tenant_run_count = sum(
                    1
                    for existing_run_id in app._events_by_run_id
                    if (
                        existing_run_id
                        in app._run_authorization_by_run_id
                        and app._run_authorization_by_run_id[
                            existing_run_id
                        ].tenant_id
                        == run_tenant_id
                    )
                ) + sum(
                    1
                    for reservation in (
                        app._accepted_run_reservations_by_run_id.values()
                    )
                    if (
                        reservation.authorization.tenant_id
                        == run_tenant_id
                    )
                )
                if (
                    in_memory_tenant_run_count
                    >= app.max_in_memory_runs_per_tenant
                ):
                    return ServerResponse.json(
                        429,
                        {
                            "ok": False,
                            "runId": run_id,
                            "tenantId": run_tenant_id,
                            "reasonCode": (
                                "server.tenant_run_capacity_exhausted"
                            ),
                            "maxInMemoryRunsPerTenant": (
                                app.max_in_memory_runs_per_tenant
                            ),
                            "error": (
                                "server tenant in-memory run capacity "
                                "is exhausted"
                            ),
                        },
                    )
                app._accepted_run_reservations_by_run_id[
                    run_id
                ] = reservation_candidate
                accepted_run_reservation = reservation_candidate

            try:
                block_catalog = app.registry.compilation_catalog()
                plan = (
                    compile_graph(graph, block_catalog=block_catalog)
                    if block_catalog is not None
                    else compile_graph(graph)
                )
            except Exception as error:
                return app._safe_error_response(
                    request,
                    route_match,
                    status_code=500,
                    error_code="server.run.compilation_failed",
                    message="The run could not be compiled.",
                    error=error,
                )
            plan_errors = [
                item
                for item in plan.diagnostics.diagnostics
                if item.severity == "error"
            ]
            if plan_errors:
                raise ValueError(
                    "; ".join(
                        f"{item.code} {item.path}: {item.message}"
                        for item in plan_errors
                    )
                )
            normalized_spec = plan.normalized.get("spec")
            normalized_nodes = (
                normalized_spec.get("nodes")
                if isinstance(normalized_spec, Mapping)
                else None
            )
            requires_durable_sync_continuation = False
            if (
                response_mode == "sync"
                and not app.allow_process_local_accepted_runs_dev
                and isinstance(normalized_nodes, Mapping)
            ):
                for node in normalized_nodes.values():
                    if (
                        not isinstance(node, Mapping)
                        or node.get("block")
                        != "async.await_callback@1"
                    ):
                        continue
                    callback_wait_config = node.get("config")
                    if (
                        isinstance(callback_wait_config, Mapping)
                        and callback_wait_config.get(
                            "checkpoint",
                            True,
                        )
                        is False
                    ):
                        continue
                    requires_durable_sync_continuation = True
                    break
            if requires_durable_sync_continuation:
                return ServerResponse.json(
                    503,
                    {
                        "ok": False,
                        "reasonCode": (
                            _DURABLE_ACCEPTED_RUN_REQUIRED_REASON_CODE
                        ),
                        "error": _DURABLE_ACCEPTED_RUN_REQUIRED_ERROR,
                    },
                )
            frozen_start_event: Mapping[str, object] | None = None
            if not ticketed_admission:
                start_event = ApplicationEvent.new(
                    "RunStarted",
                    ApplicationEventMetadata(
                        event_id=f"{run_id}:run-started",
                        run_id=run_id,
                        response_id=response_id,
                        turn_id=turn_id,
                        sequence=1,
                        release_id=release_id,
                        policy_snapshot_id=policy_snapshot_id,
                        occurred_at=occurred_at,
                        cursor=f"{run_id}:1",
                    ),
                    payload={
                        "status": "running",
                        "graph_hash": plan.graph_hash,
                    },
                )
                start_event_payload: dict[str, object] = {
                    "kind": start_event.kind,
                    "metadata": {
                        "eventId": start_event.metadata.event_id,
                        "runId": start_event.metadata.run_id,
                        "responseId": start_event.metadata.response_id,
                        "turnId": start_event.metadata.turn_id,
                        "sequence": start_event.metadata.sequence,
                        "cursor": start_event.metadata.cursor,
                        "releaseId": start_event.metadata.release_id,
                        "policySnapshotId": start_event.metadata.policy_snapshot_id,
                        "occurredAt": start_event.metadata.occurred_at,
                        "graphId": start_event.metadata.graph_id,
                        "nodeId": start_event.metadata.node_id,
                        "operationId": start_event.metadata.operation_id,
                        "visibility": start_event.metadata.visibility,
                    },
                    "payload": dict(start_event.payload),
                }
                frozen_start_event = _freeze_json_value(
                    "application event stream",
                    "event",
                    start_event_payload,
                )
                assert isinstance(frozen_start_event, Mapping)
            pending_run = _freeze_json_value(
                "server pending accepted run",
                "run",
                {
                    "graph": graph,
                    "inputs": inputs,
                    "runId": run_id,
                    "responseId": response_id,
                    "releaseId": release_id,
                    "policySnapshotId": policy_snapshot_id,
                    "turnId": turn_id,
                    "requestedAt": occurred_at,
                    "graphHash": plan.graph_hash,
                },
            )
            assert isinstance(pending_run, Mapping)
            accepted_run_cancellation_token = CancellationToken()
            accepted_run_journal = ExecutionJournal(run_id)
            assert accepted_run_reservation is not None
            accepted_run_execution = _AcceptedRunExecution(
                admission_reservation_id=(
                    accepted_run_reservation.reservation_id
                ),
                runtime=InProcessRuntime(
                    app.registry,
                    cancellation_token=accepted_run_cancellation_token,
                    journal_factory=lambda _run_id: accepted_run_journal,
                ),
                cancellation_token=accepted_run_cancellation_token,
                journal=accepted_run_journal,
            )
            deferred = (
                ticketed_admission
                or (
                    app.defer_accepted_runs
                    and response_mode in {"accepted", "background"}
                )
            )
            completion = None
            admission_ticket: AdmissionTicket | None = None
            if ticketed_admission:
                assert app.admission_ticket_queue is not None
                admission_now_ms = app.admission_clock()
                app.promote_admission_tickets(now_ms=admission_now_ms)
                with app._accepted_run_condition:
                    if (
                        app._accepted_run_reservations_by_run_id.get(
                            run_id
                        )
                        is not accepted_run_reservation
                    ):
                        raise ValueError(
                            f"run {run_id!r} admission reservation changed"
                        )
                    try:
                        submission = app.admission_ticket_queue.submit(
                            run_id,
                            request_id,
                            admission_owner_id,
                            tenant_id=run_tenant_id,
                            now_ms=admission_now_ms,
                            units=admission_units,
                        )
                    except AdmissionQueueFullError as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=429,
                            error_code="server.admission.capacity_exhausted",
                            message="Run admission capacity is exhausted.",
                            error=error,
                        )
                    except AdmissionIdempotencyConflictError as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=409,
                            error_code="server.admission.idempotency_conflict",
                            message="The admission request conflicts with an existing request.",
                            error=error,
                        )
                    admission_ticket = submission.ticket
                    try:
                        app._record_run_authorization(
                            run_id,
                            auth_decision.principal,
                            occurred_at,
                        )
                    except Exception as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=500,
                            error_code="server.run.persistence_failed",
                            message="The run could not be persisted.",
                            error=error,
                        )
                    app._events_by_run_id[run_id] = ()
                    app._pending_accepted_runs_by_run_id[run_id] = pending_run
                    app._accepted_run_executions_by_run_id[
                        run_id
                    ] = accepted_run_execution
                    app._admission_ticket_ids_by_run_id[
                        run_id
                    ] = admission_ticket.ticket_id
                    app._accepted_run_reservations_by_run_id.pop(
                        run_id,
                    )
                    accepted_run_reservation = None
                    app._accepted_run_condition.notify_all()
                if admission_ticket.state == "admitted":
                    app._dispatch_admitted_tickets((admission_ticket,))
            elif deferred and app.accepted_run_executor is not None:
                try:
                    executor_probe = app._submit_accepted_run_task(
                        get_ident
                    )
                except Exception as error:
                    return app._safe_error_response(
                        request,
                        route_match,
                        status_code=503,
                        error_code="server.accepted_run.executor_unavailable",
                        message="Accepted-run execution is unavailable.",
                        error=error,
                    )
                if executor_probe.done():
                    try:
                        executor_thread_id = executor_probe.result()
                    except Exception as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=503,
                            error_code="server.accepted_run.executor_unavailable",
                            message="Accepted-run execution is unavailable.",
                            error=error,
                        )
                    if executor_thread_id == get_ident():
                        return ServerResponse.json(
                            503,
                            {
                                "ok": False,
                                "runId": run_id,
                                "error": (
                                    "accepted run executor must execute work "
                                    "asynchronously"
                                ),
                            },
                        )
                try:
                    app._submit_accepted_run(
                        run_id,
                        _expected_admission_reservation_id=(
                            accepted_run_execution.admission_reservation_id
                        ),
                    )
                except Exception as error:
                    return app._safe_error_response(
                        request,
                        route_match,
                        status_code=503,
                        error_code="server.accepted_run.executor_unavailable",
                        message="Accepted-run execution is unavailable.",
                        error=error,
                    )
                with app._accepted_run_condition:
                    if (
                        app._accepted_run_reservations_by_run_id.get(
                            run_id
                        )
                        is not accepted_run_reservation
                    ):
                        raise ValueError(
                            f"run {run_id!r} admission reservation changed"
                        )
                    assert frozen_start_event is not None
                    try:
                        app._record_run_authorization(
                            run_id,
                            auth_decision.principal,
                            occurred_at,
                        )
                    except Exception as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=500,
                            error_code="server.run.persistence_failed",
                            message="The run could not be persisted.",
                            error=error,
                        )
                    app._events_by_run_id[run_id] = (
                        frozen_start_event,
                    )
                    app._pending_accepted_runs_by_run_id[
                        run_id
                    ] = pending_run
                    app._accepted_run_executions_by_run_id[
                        run_id
                    ] = accepted_run_execution
                    app._accepted_run_reservations_by_run_id.pop(
                        run_id,
                    )
                    accepted_run_reservation = None
                    app._accepted_run_condition.notify_all()
            else:
                with app._accepted_run_condition:
                    if (
                        app._accepted_run_reservations_by_run_id.get(
                            run_id
                        )
                        is not accepted_run_reservation
                    ):
                        raise ValueError(
                            f"run {run_id!r} admission reservation changed"
                        )
                    assert frozen_start_event is not None
                    try:
                        app._record_run_authorization(
                            run_id,
                            auth_decision.principal,
                            occurred_at,
                        )
                    except Exception as error:
                        return app._safe_error_response(
                            request,
                            route_match,
                            status_code=500,
                            error_code="server.run.persistence_failed",
                            message="The run could not be persisted.",
                            error=error,
                        )
                    app._events_by_run_id[run_id] = (frozen_start_event,)
                    app._pending_accepted_runs_by_run_id[run_id] = pending_run
                    app._accepted_run_executions_by_run_id[
                        run_id
                    ] = accepted_run_execution
                    app._accepted_run_reservations_by_run_id.pop(
                        run_id,
                    )
                    app._accepted_run_condition.notify_all()
                    accepted_run_reservation = None
            if not deferred:
                completion = app.advance_accepted_run(
                    run_id,
                    completed_at=occurred_at,
                    _lifecycle_admitted=True,
                )
            if response_mode in {"accepted", "background"}:
                route_run_id = quote(run_id, safe="")
                accepted_payload: dict[str, object] = {
                    "ok": True,
                    "runId": run_id,
                    "status": response_mode,
                    "eventStream": f"/runs/{route_run_id}/events",
                    "websocket": f"/runs/{route_run_id}/ws",
                    "cancel": f"/runs/{route_run_id}/cancel",
                    "initialCursor": f"{run_id}:0",
                }
                if admission_ticket is not None:
                    refreshed_ticket = admission_ticket
                    if app.admission_ticket_queue is not None:
                        try:
                            refreshed_ticket = (
                                app.admission_ticket_queue.get(
                                    admission_ticket.ticket_id
                                )
                            )
                        except AdmissionTicketNotFoundError:
                            # The ticket completed and was pruned before
                            # the response was assembled; fall back to
                            # the snapshot taken at admission.
                            refreshed_ticket = admission_ticket
                    accepted_payload["admissionTicket"] = (
                        refreshed_ticket.contract()
                    )
                return ServerResponse.json(
                    202,
                    accepted_payload,
                )
            assert completion is not None
            route_run_id = quote(run_id, safe="")
            retained_events = app._events_by_run_id[run_id]
            return ServerResponse.json(
                200,
                {
                    "runId": run_id,
                    "status": completion["status"],
                    "outputs": completion["outputs"],
                    "eventCount": len(retained_events),
                    "lastCursor": (
                        f"{run_id}:{app._last_event_sequence(retained_events)}"
                    ),
                    "eventStream": f"/runs/{route_run_id}/events",
                    "websocket": f"/runs/{route_run_id}/ws",
                },
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.run.invalid_request",
                message="The run request is invalid.",
                error=error,
            )
        finally:
            if accepted_run_reservation is not None:
                with app._accepted_run_condition:
                    reservation_run_id = (
                        accepted_run_reservation.authorization.external_run_id
                    )
                    if (
                        app._accepted_run_reservations_by_run_id.get(
                            reservation_run_id
                        )
                        is accepted_run_reservation
                    ):
                        app._accepted_run_reservations_by_run_id.pop(
                            reservation_run_id
                        )
                        app._accepted_run_condition.notify_all()
    return ServerResponse.json(
        501,
        {
            "ok": False,
            "error": (
                f"server operation {route.operation!r} is not implemented"
            ),
        },
    )
