"""Server application-protocol event and stream operation handlers.

Extracted from ``graphblocks.server``: the ``application_events`` and
``application_stream`` dispatch-table entries now point at the free
functions below, whose bodies are the former ``GraphBlocksServerApp``
methods moved verbatim with ``self`` renamed to ``app``.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import TYPE_CHECKING

from ._server_auth import ServerAuthDecision
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_validation import (
    _event_visible_to_principal,
    _response_json_object,
    _validate_run_cursor,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


def _handle_application_events_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "application_events":
        run_id = route_match.path_params.get("run_id", "")
        if not app._principal_can_access_run(run_id, auth_decision.principal):
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run events not found for run {run_id!r}",
                },
            )
        events = app._events_by_run_id.get(run_id)
        if events is None:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run events not found for run {run_id!r}",
                },
            )
        try:
            cursor = request.query.get("cursor")
            if cursor is not None:
                cursor = _validate_run_cursor("application events", "cursor", run_id, cursor)
            event_page_event_limit = app.max_event_page_events
            event_page_byte_limit = app.max_event_page_bytes
            for query_name, configured_limit in (
                ("maxEvents", app.max_event_page_events),
                ("maxBytes", app.max_event_page_bytes),
            ):
                query_value = request.query.get(query_name)
                if query_value is None:
                    continue
                if query_value != query_value.strip():
                    raise ValueError(
                        f"application events {query_name} must not contain surrounding whitespace"
                    )
                if not query_value.isascii() or not query_value.isdecimal():
                    raise ValueError(
                        f"application events {query_name} must be a positive ASCII integer"
                    )
                if len(query_value) > len(str(configured_limit)):
                    raise ValueError(
                        f"application events {query_name} must be at most {configured_limit}"
                    )
                query_limit = int(query_value)
                if query_limit < 1:
                    raise ValueError(
                        f"application events {query_name} must be a positive ASCII integer"
                    )
                if query_limit > configured_limit:
                    raise ValueError(
                        f"application events {query_name} must be at most {configured_limit}"
                    )
                if query_name == "maxEvents":
                    event_page_event_limit = query_limit
                else:
                    event_page_byte_limit = query_limit
            requested_sequence = (
                int(cursor[len(run_id) + 1 :])
                if cursor is not None
                else 0
            )
            cursor_found = cursor is None or requested_sequence == 0
            nearest_sequence: int | None = None
            last_sequence = 0
            previous_sequence: int | None = None
            for retained_event in events:
                retained_metadata = retained_event.get("metadata")
                if not isinstance(retained_metadata, Mapping):
                    continue
                retained_sequence = retained_metadata.get("sequence")
                if not isinstance(retained_sequence, int) or isinstance(
                    retained_sequence,
                    bool,
                ):
                    raise ValueError("application events sequence must be an integer")
                if retained_sequence < 0:
                    raise ValueError("application events sequence must be non-negative")
                if (
                    previous_sequence is not None
                    and retained_sequence <= previous_sequence
                ):
                    raise ValueError(
                        "application events sequence must be strictly increasing"
                    )
                previous_sequence = retained_sequence
                if (
                    nearest_sequence is None
                    or retained_sequence < nearest_sequence
                ):
                    nearest_sequence = retained_sequence
                if retained_sequence > last_sequence:
                    last_sequence = retained_sequence
                if retained_sequence == requested_sequence:
                    cursor_found = True
            if cursor is not None and not cursor_found:
                nearest_cursor = (
                    f"{run_id}:{nearest_sequence}"
                    if nearest_sequence is not None
                    else None
                )
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "error": "CursorExpired",
                        "runId": run_id,
                        "requestedCursor": cursor,
                        "nearestAvailableCursor": nearest_cursor,
                        "lastCursor": f"{run_id}:{last_sequence}",
                        "lastSequence": last_sequence,
                        "runStatus": app._run_status_payload(run_id, events, include_ok=False),
                    },
                )
        except ValueError as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.events.invalid_request",
                message="The event request is invalid.",
                error=error,
            )
        page_candidates: list[tuple[dict[str, object], str]] = []
        for candidate_event in events:
            candidate_metadata = candidate_event.get("metadata")
            if not isinstance(candidate_metadata, Mapping):
                continue
            candidate_sequence = candidate_metadata.get("sequence")
            if (
                not isinstance(candidate_sequence, int)
                or isinstance(candidate_sequence, bool)
                or candidate_sequence < 0
                or candidate_sequence <= requested_sequence
                or not _event_visible_to_principal(
                    candidate_event,
                    auth_decision.principal,
                )
            ):
                continue
            page_candidates.append(
                (
                    _response_json_object(candidate_event),
                    f"{run_id}:{candidate_sequence}",
                )
            )
            if len(page_candidates) > event_page_event_limit:
                break

        event_page_last_cursor = f"{run_id}:{last_sequence}"
        if not page_candidates:
            empty_event_page_response = ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "replayFromCursor": cursor,
                    "lastCursor": event_page_last_cursor,
                    "hasMore": False,
                    "nextCursor": None,
                    "events": [],
                },
            )
            if (
                len(empty_event_page_response.body)
                <= event_page_byte_limit
            ):
                return empty_event_page_response
            return ServerResponse.json(
                413,
                {
                    "ok": False,
                    "error": "EventPageTooLarge",
                    "runId": run_id,
                    "maxBytes": event_page_byte_limit,
                    "requiredBytes": len(empty_event_page_response.body),
                    "nextEventCursor": None,
                },
            )

        prefix_event_bytes = 0
        candidate_sizes: list[int] = []
        available_count = min(
            event_page_event_limit,
            len(page_candidates),
        )
        for candidate_index in range(available_count):
            candidate_event_payload, candidate_event_cursor = (
                page_candidates[candidate_index]
            )
            encoded_candidate_event = json.dumps(
                candidate_event_payload,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            prefix_event_bytes += len(encoded_candidate_event)
            candidate_count = candidate_index + 1
            candidate_has_more = candidate_count < len(page_candidates)
            candidate_envelope = ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "replayFromCursor": cursor,
                    "lastCursor": event_page_last_cursor,
                    "hasMore": candidate_has_more,
                    "nextCursor": (
                        candidate_event_cursor
                        if candidate_has_more
                        else None
                    ),
                    "events": [],
                },
            )
            candidate_sizes.append(
                len(candidate_envelope.body)
                + prefix_event_bytes
                + max(0, candidate_count - 1)
            )

        page_event_count = 0
        for candidate_count, candidate_size in enumerate(
            candidate_sizes,
            start=1,
        ):
            if candidate_size <= event_page_byte_limit:
                page_event_count = candidate_count
        if page_event_count == 0:
            return ServerResponse.json(
                413,
                {
                    "ok": False,
                    "error": "EventPageTooLarge",
                    "runId": run_id,
                    "maxBytes": event_page_byte_limit,
                    "requiredBytes": candidate_sizes[0],
                    "nextEventCursor": page_candidates[0][1],
                },
            )

        event_page_has_more = page_event_count < len(page_candidates)
        event_page_next_cursor = (
            page_candidates[page_event_count - 1][1]
            if event_page_has_more
            else None
        )
        return ServerResponse.json(
            200,
            {
                "ok": True,
                "runId": run_id,
                "replayFromCursor": cursor,
                "lastCursor": event_page_last_cursor,
                "hasMore": event_page_has_more,
                "nextCursor": event_page_next_cursor,
                "events": [
                    page_event_payload
                    for page_event_payload, _ in page_candidates[:page_event_count]
                ],
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


def _handle_application_stream_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "application_stream":
        run_id = route_match.path_params.get("run_id", "")
        if request.headers.get("upgrade", "").lower() != "websocket" or (
            "upgrade" not in request.headers.get("connection", "").lower()
        ):
            return ServerResponse(
                status_code=426,
                headers={"content-type": "application/json", "upgrade": "websocket"},
                body=json.dumps(
                    {
                        "ok": False,
                        "error": "application stream requires websocket upgrade",
                        "runId": run_id,
                        "requiredTransport": "websocket",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
            )
        if not app._principal_can_access_run(run_id, auth_decision.principal):
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"application stream not found for run {run_id!r}",
                },
            )
        events = app._events_by_run_id.get(run_id)
        if events is None:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"application stream not found for run {run_id!r}",
                },
            )
        try:
            cursor = request.query.get("cursor")
            if cursor is not None:
                cursor = _validate_run_cursor(
                    "application stream",
                    "cursor",
                    run_id,
                    cursor,
                )
            requested_sequence = (
                int(cursor[len(run_id) + 1 :])
                if cursor is not None
                else 0
            )
            cursor_found = cursor is None or requested_sequence == 0
            nearest_sequence: int | None = None
            last_sequence = 0
            previous_sequence: int | None = None
            for event in events:
                metadata = event.get("metadata")
                if not isinstance(metadata, Mapping):
                    continue
                sequence = metadata.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    raise ValueError(
                        "application stream sequence must be an integer"
                    )
                if sequence < 0:
                    raise ValueError(
                        "application stream sequence must be non-negative"
                    )
                if (
                    previous_sequence is not None
                    and sequence <= previous_sequence
                ):
                    raise ValueError(
                        "application stream sequence must be strictly increasing"
                    )
                previous_sequence = sequence
                if nearest_sequence is None or sequence < nearest_sequence:
                    nearest_sequence = sequence
                if sequence > last_sequence:
                    last_sequence = sequence
                if sequence == requested_sequence:
                    cursor_found = True
            if cursor is not None and not cursor_found:
                nearest_cursor = (
                    f"{run_id}:{nearest_sequence}"
                    if nearest_sequence is not None
                    else None
                )
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "error": "CursorExpired",
                        "runId": run_id,
                        "requestedCursor": cursor,
                        "nearestAvailableCursor": nearest_cursor,
                        "lastCursor": f"{run_id}:{last_sequence}",
                        "lastSequence": last_sequence,
                        "runStatus": app._run_status_payload(
                            run_id,
                            events,
                            include_ok=False,
                        ),
                    },
                )
        except ValueError as error:
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.application_stream.invalid_request",
                message="The application stream request is invalid.",
                error=error,
            )

        page_candidates: list[tuple[dict[str, object], str]] = []
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 0
                or sequence <= requested_sequence
                or not _event_visible_to_principal(
                    event,
                    auth_decision.principal,
                )
            ):
                continue
            page_candidates.append(
                (
                    _response_json_object(event),
                    f"{run_id}:{sequence}",
                )
            )
            if len(page_candidates) > app.max_event_page_events:
                break

        stream_last_cursor = f"{run_id}:{last_sequence}"
        if not page_candidates:
            empty_stream_response = ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "replayFromCursor": cursor,
                    "lastCursor": stream_last_cursor,
                    "hasMore": False,
                    "nextCursor": None,
                    "stream": {
                        "transport": "websocket",
                        "status": "accepted",
                        "cursor": stream_last_cursor,
                        "eventCount": 0,
                    },
                    "events": [],
                },
            )
            if (
                len(empty_stream_response.body)
                <= app.max_event_page_bytes
            ):
                return empty_stream_response
            return ServerResponse.json(
                413,
                {
                    "ok": False,
                    "error": "StreamSnapshotTooLarge",
                    "reasonCode": (
                        "server.stream_snapshot_capacity_exhausted"
                    ),
                    "runId": run_id,
                    "maxBytes": app.max_event_page_bytes,
                    "requiredBytes": len(empty_stream_response.body),
                    "nextEventCursor": None,
                },
            )

        prefix_event_bytes = 0
        candidate_sizes: list[int] = []
        available_count = min(
            app.max_event_page_events,
            len(page_candidates),
        )
        for candidate_index in range(available_count):
            candidate_event, candidate_cursor = page_candidates[
                candidate_index
            ]
            encoded_candidate_event = json.dumps(
                candidate_event,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            prefix_event_bytes += len(encoded_candidate_event)
            candidate_count = candidate_index + 1
            candidate_has_more = candidate_count < len(
                page_candidates
            )
            candidate_envelope = ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "replayFromCursor": cursor,
                    "lastCursor": stream_last_cursor,
                    "hasMore": candidate_has_more,
                    "nextCursor": (
                        candidate_cursor
                        if candidate_has_more
                        else None
                    ),
                    "stream": {
                        "transport": "websocket",
                        "status": "accepted",
                        "cursor": stream_last_cursor,
                        "eventCount": candidate_count,
                    },
                    "events": [],
                },
            )
            candidate_sizes.append(
                len(candidate_envelope.body)
                + prefix_event_bytes
                + max(0, candidate_count - 1)
            )

        page_event_count = 0
        for candidate_count, candidate_size in enumerate(
            candidate_sizes,
            start=1,
        ):
            if candidate_size <= app.max_event_page_bytes:
                page_event_count = candidate_count
        if page_event_count == 0:
            return ServerResponse.json(
                413,
                {
                    "ok": False,
                    "error": "StreamSnapshotTooLarge",
                    "reasonCode": (
                        "server.stream_snapshot_capacity_exhausted"
                    ),
                    "runId": run_id,
                    "maxBytes": app.max_event_page_bytes,
                    "requiredBytes": candidate_sizes[0],
                    "nextEventCursor": page_candidates[0][1],
                },
            )

        stream_has_more = page_event_count < len(page_candidates)
        stream_next_cursor = (
            page_candidates[page_event_count - 1][1]
            if stream_has_more
            else None
        )
        return ServerResponse.json(
            200,
            {
                "ok": True,
                "runId": run_id,
                "replayFromCursor": cursor,
                "lastCursor": stream_last_cursor,
                "hasMore": stream_has_more,
                "nextCursor": stream_next_cursor,
                "stream": {
                    "transport": "websocket",
                    "status": "accepted",
                    "cursor": stream_last_cursor,
                    "eventCount": page_event_count,
                },
                "events": [
                    event
                    for event, _ in page_candidates[:page_event_count]
                ],
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
