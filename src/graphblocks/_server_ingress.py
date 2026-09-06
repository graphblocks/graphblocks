"""Server async-callback ingress operation handler.

Extracted from ``graphblocks.server``: the ``async_callback_ingress``
dispatch-table entry now points at the free function below, whose body is
the former ``GraphBlocksServerApp`` method moved verbatim with ``self``
renamed to ``app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING

from ._canonical_reference import canonical_dumps, canonical_hash
from ._server_auth import (
    _AcceptedCallbackReceiptCapability,
    ServerAuthDecision,
)
from ._server_callbacks import (
    _callback_alias_value,
    _callback_idempotency_key,
    _optional_callback_string,
    ServerAsyncCallbackRejection,
    ServerAsyncCallbackSubmission,
)
from ._server_transport import (
    ServerRequest,
    ServerResponse,
    ServerRouteMatch,
)
from ._server_validation import (
    _freeze_json_value,
    _SERVER_BEARER_CHALLENGE,
    _server_request_json_body,
    _thaw_json_value,
    _utc_now_iso,
    _validate_exact_non_empty_string,
    _validate_iso_datetime,
)

if TYPE_CHECKING:
    from .server import GraphBlocksServerApp


def _handle_async_callback_ingress_operation(
    app: GraphBlocksServerApp,
    request: ServerRequest,
    route_match: ServerRouteMatch,
    auth_decision: ServerAuthDecision,
) -> ServerResponse:
    route = route_match.endpoint
    if route.operation == "submit_async_callback":
        callback_state_locked = False
        try:
            submission = ServerAsyncCallbackSubmission.from_request(
                operation_id=route_match.path_params.get("operation_id", ""),
                request=request,
                verified_by=(
                    auth_decision.principal.principal_id
                    if auth_decision.principal is not None
                    else "unauthenticated"
                ),
            )
            if (
                submission.run_id is not None
                and not app._principal_can_access_run(
                    submission.run_id,
                    auth_decision.principal,
                )
            ):
                if app.anti_enumerate_async_callbacks:
                    return ServerResponse.json(
                        202,
                        {
                            "ok": True,
                            "status": "accepted",
                        },
                    )
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": "async callback target not found",
                    },
                )
            payload_size_bytes = len(canonical_dumps(_thaw_json_value(submission.payload)).encode("utf-8"))
            if payload_size_bytes > app.max_async_callback_payload_bytes:
                rejection = ServerAsyncCallbackRejection.payload_too_large(submission)
                app._async_callback_rejections_by_operation_id.append(
                    rejection
                )
                return ServerResponse.json(
                    413,
                    {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "payloadSizeBytes": payload_size_bytes,
                        "maxPayloadBytes": app.max_async_callback_payload_bytes,
                        "error": "async callback payload exceeds max payload bytes",
                    },
                )
            for previous in app._callbacks_by_operation_id.get(
                submission.operation_id,
                (),
            ):
                if (
                    previous.idempotency_key == submission.idempotency_key
                    and previous.callback_id == submission.callback_id
                    and dict(previous.payload) == dict(submission.payload)
                    and previous.artifacts == submission.artifacts
                    and previous.run_id == submission.run_id
                    and previous.node_id == submission.node_id
                    and previous.attempt_id == submission.attempt_id
                    and previous.provider_operation_id
                    == submission.provider_operation_id
                    and previous.verified_by == submission.verified_by
                    and previous.policy_snapshot_id
                    == submission.policy_snapshot_id
                ):
                    return ServerResponse.json(
                        200,
                        previous.duplicate_response_payload(),
                    )
            if submission.run_id is not None and submission.run_id not in app._events_by_run_id:
                rejection = ServerAsyncCallbackRejection.unknown_run(submission)
                app._async_callback_rejections_by_operation_id.append(
                    rejection
                )
                if app.anti_enumerate_async_callbacks:
                    return ServerResponse.json(
                        202,
                        {
                            "ok": True,
                            "status": "accepted",
                        },
                    )
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "runId": submission.run_id,
                        "error": f"async callback run {submission.run_id!r} not found",
                    },
                )
            if submission.run_id is not None and submission.attempt_id is None:
                rejection = ServerAsyncCallbackRejection.missing_attempt_fence(submission)
                app._async_callback_rejections_by_operation_id.append(
                    rejection
                )
                app._append_async_callback_diagnostic_event(
                    "ExternalCallbackRejected",
                    submission,
                    "missing_attempt_fence",
                )
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "runId": submission.run_id,
                        "error": "async callback attempt_id is required when run_id is declared",
                    },
                )
            if submission.run_id is not None and submission.node_id is None:
                rejection = ServerAsyncCallbackRejection.missing_node_fence(submission)
                app._async_callback_rejections_by_operation_id.append(
                    rejection
                )
                app._append_async_callback_diagnostic_event(
                    "ExternalCallbackRejected",
                    submission,
                    "missing_node_fence",
                )
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "runId": submission.run_id,
                        "error": "async callback node_id is required when run_id is declared",
                    },
                )
            if submission.run_id is not None:
                run_status = app._run_status_payload(
                    submission.run_id,
                    app._events_by_run_id[submission.run_id],
                    include_ok=False,
                )
                state = run_status.get("state")
                if state in {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}:
                    rejection = ServerAsyncCallbackRejection.terminal_run(submission, state)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "LateExternalCallbackReceived",
                        submission,
                        "terminal_run",
                        status=state if isinstance(state, str) else None,
                    )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "status": state,
                            "error": "async callback run is terminal and cannot be resumed",
                        },
                    )
            app._accepted_run_condition.acquire()
            callback_state_locked = True
            if submission.run_id is not None:
                current_events = app._events_by_run_id.get(submission.run_id)
                if current_events is None:
                    rejection = ServerAsyncCallbackRejection.unknown_run(
                        submission
                    )
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    if app.anti_enumerate_async_callbacks:
                        return ServerResponse.json(
                            202,
                            {
                                "ok": True,
                                "status": "accepted",
                            },
                        )
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                f"async callback run {submission.run_id!r} "
                                "not found"
                            ),
                        },
                    )
                current_status = app._run_status_payload(
                    submission.run_id,
                    current_events,
                    include_ok=False,
                )
                current_state = current_status.get("state")
                if current_state in {
                    "completed",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                    "policy_stopped",
                }:
                    rejection = ServerAsyncCallbackRejection.terminal_run(
                        submission,
                        current_state,
                    )
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "LateExternalCallbackReceived",
                        submission,
                        "terminal_run",
                        status=(
                            current_state
                            if isinstance(current_state, str)
                            else None
                        ),
                    )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "status": current_state,
                            "error": (
                                "async callback run is terminal and cannot "
                                "be resumed"
                            ),
                        },
                    )
            if submission.run_id is not None:
                pending_execution = app._accepted_run_executions_by_run_id.get(
                    submission.run_id
                )
                if (
                    pending_execution is not None
                    and pending_execution.checkpoint is None
                ):
                    rejection = ServerAsyncCallbackRejection(
                        operation_id=submission.operation_id,
                        callback_id=submission.callback_id,
                        idempotency_key=submission.idempotency_key,
                        reason="checkpoint_not_published",
                        received_at=submission.received_at,
                        payload_digest=submission.payload_digest,
                        verified_by=submission.verified_by,
                        policy_snapshot_id=submission.policy_snapshot_id,
                        run_id=submission.run_id,
                        node_id=submission.node_id,
                        attempt_id=submission.attempt_id,
                        provider_operation_id=(
                            submission.provider_operation_id
                        ),
                    )
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                "async callback run has not published a callback checkpoint"
                            ),
                        },
                    )
            existing = app._callbacks_by_operation_id.get(submission.operation_id, ())
            for previous in existing:
                if previous.idempotency_key == submission.idempotency_key:
                    if (
                        previous.callback_id != submission.callback_id
                        or dict(previous.payload) != dict(submission.payload)
                        or previous.artifacts != submission.artifacts
                        or previous.run_id != submission.run_id
                        or previous.node_id != submission.node_id
                        or previous.attempt_id != submission.attempt_id
                        or previous.provider_operation_id != submission.provider_operation_id
                        or previous.verified_by != submission.verified_by
                        or previous.policy_snapshot_id
                        != submission.policy_snapshot_id
                    ):
                        rejection = ServerAsyncCallbackRejection.idempotency_conflict(submission)
                        app._async_callback_rejections_by_operation_id.append(
                            rejection
                        )
                        app._append_async_callback_diagnostic_event(
                            "ExternalCallbackRejected",
                            submission,
                            "idempotency_conflict",
                        )
                        return ServerResponse.json(
                            409,
                            {
                                "ok": False,
                                "operationId": submission.operation_id,
                                "idempotencyKey": submission.idempotency_key,
                                "error": "async callback idempotency key was reused with different content",
                            },
                        )
                    return ServerResponse.json(200, previous.duplicate_response_payload())
                if (previous.run_id is None) != (submission.run_id is None):
                    payload: dict[str, object] = {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "error": "async callback operation scope cannot change after first receipt",
                    }
                    if submission.run_id is not None:
                        payload["runId"] = submission.run_id
                        if submission.attempt_id is not None:
                            payload["attemptId"] = submission.attempt_id
                    rejection = ServerAsyncCallbackRejection.scope_mismatch(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "scope_mismatch",
                    )
                    return ServerResponse.json(409, payload)
                if previous.attempt_id != submission.attempt_id:
                    rejection = ServerAsyncCallbackRejection.stale_attempt(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "stale_attempt",
                    )
                    payload = {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "error": "async callback operation is already bound to a different run attempt",
                    }
                    if submission.run_id is not None:
                        payload["runId"] = submission.run_id
                    if submission.attempt_id is not None:
                        payload["attemptId"] = submission.attempt_id
                    return ServerResponse.json(409, payload)
                if (
                    previous.run_id is not None
                    and submission.run_id is not None
                    and (previous.run_id != submission.run_id or previous.attempt_id != submission.attempt_id)
                ):
                    rejection = ServerAsyncCallbackRejection.stale_attempt(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "stale_attempt",
                    )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "attemptId": submission.attempt_id,
                            "error": "async callback operation is already bound to a different run attempt",
                        },
                    )
                if (
                    previous.run_id is not None
                    and submission.run_id is not None
                    and previous.node_id != submission.node_id
                ):
                    rejection = ServerAsyncCallbackRejection.node_mismatch(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "node_mismatch",
                    )
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "attemptId": submission.attempt_id,
                            "nodeId": submission.node_id,
                            "error": "async callback operation is already bound to a different run node attempt",
                        },
                    )
                if previous.provider_operation_id != submission.provider_operation_id:
                    rejection = ServerAsyncCallbackRejection.provider_operation_mismatch(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "provider_operation_mismatch",
                    )
                    payload = {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "error": "async callback operation is already bound to a different provider operation",
                    }
                    if submission.run_id is not None:
                        payload["runId"] = submission.run_id
                    if submission.attempt_id is not None:
                        payload["attemptId"] = submission.attempt_id
                    if submission.node_id is not None:
                        payload["nodeId"] = submission.node_id
                    if submission.provider_operation_id is not None:
                        payload["providerOperationId"] = submission.provider_operation_id
                    return ServerResponse.json(409, payload)
                rejection = ServerAsyncCallbackRejection.duplicate_operation_receipt(submission)
                app._async_callback_rejections_by_operation_id.append(
                    rejection
                )
                app._append_async_callback_diagnostic_event(
                    "ExternalCallbackRejected",
                    submission,
                    "duplicate_operation_receipt",
                )
                payload = {
                    "ok": False,
                    "operationId": submission.operation_id,
                    "error": "async callback operation already has a recorded receipt",
                }
                if submission.run_id is not None:
                    payload["runId"] = submission.run_id
                if submission.attempt_id is not None:
                    payload["attemptId"] = submission.attempt_id
                if submission.node_id is not None:
                    payload["nodeId"] = submission.node_id
                return ServerResponse.json(409, payload)
            retained_submission_bytes = submission._retained_size_bytes()
            if (
                len(app._callbacks_by_operation_id)
                >= app.max_callback_operation_histories
                or (
                    app._callback_submission_history_bytes
                    + retained_submission_bytes
                    > app.max_callback_submission_history_bytes
                )
            ):
                return ServerResponse.json(
                    429,
                    {
                        "ok": False,
                        "operationId": submission.operation_id,
                        "reasonCode": (
                            "server.callback_submission_capacity_exhausted"
                        ),
                        "maxCallbackOperationHistories": (
                            app.max_callback_operation_histories
                        ),
                        "maxCallbackSubmissionHistoryBytes": (
                            app.max_callback_submission_history_bytes
                        ),
                        "error": (
                            "server callback submission history capacity "
                            "is exhausted"
                        ),
                    },
                )
            resumable_execution = (
                app._accepted_run_executions_by_run_id.get(
                    submission.run_id
                )
                if submission.run_id is not None
                else None
            )
            if (
                resumable_execution is not None
                and resumable_execution.checkpoint is not None
            ):
                checkpoint = resumable_execution.checkpoint
                operation = checkpoint.operation
                if submission.verified_by == "unauthenticated":
                    rejection = ServerAsyncCallbackRejection.authentication_failed(
                        submission
                    )
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                    app._append_async_callback_diagnostic_event(
                        "ExternalCallbackRejected",
                        submission,
                        "authentication_failed",
                    )
                    return ServerResponse.json(
                        401,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": "resumable async callback requires authenticated principal",
                        },
                        headers={
                            "www-authenticate": _SERVER_BEARER_CHALLENGE,
                        },
                    )
                for submission_field, operation_field in (
                    ("operation_id", "operation_id"),
                    ("run_id", "run_id"),
                    ("node_id", "node_id"),
                    ("attempt_id", "attempt_id"),
                    ("provider_operation_id", "provider_operation_id"),
                ):
                    if getattr(submission, submission_field) != operation.get(
                        operation_field
                    ):
                        rejection = ServerAsyncCallbackRejection(
                            operation_id=submission.operation_id,
                            callback_id=submission.callback_id,
                            idempotency_key=submission.idempotency_key,
                            reason="checkpoint_operation_mismatch",
                            received_at=submission.received_at,
                            payload_digest=submission.payload_digest,
                            verified_by=submission.verified_by,
                            policy_snapshot_id=submission.policy_snapshot_id,
                            run_id=submission.run_id,
                            node_id=submission.node_id,
                            attempt_id=submission.attempt_id,
                            provider_operation_id=(
                                submission.provider_operation_id
                            ),
                        )
                        app._async_callback_rejections_by_operation_id.append(
                            rejection
                        )
                        app._append_async_callback_diagnostic_event(
                            "ExternalCallbackRejected",
                            submission,
                            "checkpoint_operation_mismatch",
                        )
                        return ServerResponse.json(
                            409,
                            {
                                "ok": False,
                                "operationId": submission.operation_id,
                                "runId": submission.run_id,
                                "error": (
                                    "async callback does not match waiting checkpoint "
                                    f"{submission_field}"
                                ),
                            },
                        )
                received_datetime = datetime.fromisoformat(
                    f"{submission.received_at[:-1]}+00:00"
                    if submission.received_at.endswith("Z")
                    else submission.received_at
                ).astimezone(timezone.utc)
                waiting_events = app._events_by_run_id.get(
                    submission.run_id,
                    (),
                )
                waiting_event = next(
                    (
                        event
                        for event in reversed(waiting_events)
                        if event.get("kind")
                        == "AsyncOperationWaitingCallback"
                    ),
                    None,
                )
                waiting_metadata = (
                    waiting_event.get("metadata")
                    if isinstance(waiting_event, Mapping)
                    else None
                )
                waiting_occurred_at = (
                    waiting_metadata.get("occurredAt")
                    if isinstance(waiting_metadata, Mapping)
                    else None
                )
                waiting_occurred_at = _validate_iso_datetime(
                    "server async callback resume",
                    "checkpoint_occurred_at",
                    waiting_occurred_at,
                )
                waiting_datetime = datetime.fromisoformat(
                    f"{waiting_occurred_at[:-1]}+00:00"
                    if waiting_occurred_at.endswith("Z")
                    else waiting_occurred_at
                ).astimezone(timezone.utc)
                if received_datetime < waiting_datetime:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                "async callback receipt must not precede "
                                "callback checkpoint publication"
                            ),
                        },
                    )
                received_at_unix_ms = int(
                    received_datetime.timestamp() * 1000
                )
                submitted_at_unix_ms = operation.get(
                    "submitted_at_unix_ms"
                )
                if (
                    isinstance(submitted_at_unix_ms, int)
                    and not isinstance(submitted_at_unix_ms, bool)
                    and received_at_unix_ms < submitted_at_unix_ms
                ):
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                "async callback receipt must not precede "
                                "operation submission"
                            ),
                        },
                    )
                expires_at_unix_ms = operation.get(
                    "expires_at_unix_ms"
                )
                if (
                    isinstance(expires_at_unix_ms, int)
                    and not isinstance(expires_at_unix_ms, bool)
                    and received_at_unix_ms >= expires_at_unix_ms
                ):
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                "async callback receipt exceeds operation expiration"
                            ),
                        },
                    )
                pending_run = app._pending_accepted_runs_by_run_id.get(
                    submission.run_id
                )
                expected_policy_snapshot_id = (
                    pending_run.get("policySnapshotId")
                    if pending_run is not None
                    else None
                )
                if (
                    submission.policy_snapshot_id
                    != expected_policy_snapshot_id
                ):
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": "async callback policy snapshot does not match waiting run",
                        },
                    )
                admission_hook = app.async_callback_resume_admission_hook
                if admission_hook is None:
                    return ServerResponse.json(
                        503,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": "async callback resume admission is unavailable",
                        },
                    )
                try:
                    admission = admission_hook.admit(
                        submission,
                        checkpoint,
                    )
                except Exception as error:
                    return app._safe_error_response(
                        request,
                        route_match,
                        status_code=403,
                        error_code="server.callback.resume_admission_denied",
                        message="Callback resume admission was denied.",
                        error=error,
                    )
                required_admission = {
                    "schema_validated",
                    "policy_reevaluated",
                    "budget_reserved",
                    "release_compatible",
                    "ownership_fenced",
                }
                if not isinstance(admission, Mapping) or any(
                    admission.get(field_name) is not True
                    for field_name in required_admission
                ):
                    return ServerResponse.json(
                        403,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": (
                                "async callback resume admission requires schema, "
                                "policy, budget, release, and ownership evidence"
                            ),
                        },
                    )
                callback_receipt = _freeze_json_value(
                    "server async callback resume",
                    "receipt",
                    {
                        "operation_id": submission.operation_id,
                        "run_id": submission.run_id,
                        "node_id": submission.node_id,
                        "attempt_id": submission.attempt_id,
                        "provider_operation_id": (
                            submission.provider_operation_id
                        ),
                        "operation_idempotency_key": operation[
                            "idempotency_key"
                        ],
                        "callback_idempotency_key": (
                            submission.idempotency_key
                        ),
                        "resume_token_hash": operation[
                            "resume_token_hash"
                        ],
                        "schema_id": operation["expected_schema"],
                        "schema_validated": admission[
                            "schema_validated"
                        ],
                        "payload": _thaw_json_value(submission.payload),
                        "payload_digest": submission.payload_digest,
                        "received_at_unix_ms": received_at_unix_ms,
                        "verified_by": submission.verified_by,
                        "resume_admission": {
                            "policy_reevaluated": admission[
                                "policy_reevaluated"
                            ],
                            "budget_reserved": admission[
                                "budget_reserved"
                            ],
                            "release_compatible": admission[
                                "release_compatible"
                            ],
                            "ownership_fenced": admission[
                                "ownership_fenced"
                            ],
                        },
                    },
                )
                assert isinstance(callback_receipt, Mapping)
                resumable_execution.runtime.callback_receipt_verifier = (
                    _AcceptedCallbackReceiptCapability(
                        receipt_digest=canonical_hash(
                            _thaw_json_value(callback_receipt)
                        ),
                        checkpoint_id=checkpoint.checkpoint_id,
                        checkpoint_state_digest=checkpoint.state_digest,
                        release_digest=checkpoint.graph_hash,
                    )
                )
                resumable_execution.callback_receipt = callback_receipt
            app._callbacks_by_operation_id.setdefault(
                submission.operation_id,
                [],
            ).append(submission)
            app._callback_submission_history_bytes += (
                retained_submission_bytes
            )
            app._append_async_callback_diagnostic_event(
                "ExternalCallbackReceived",
                submission,
                None,
            )
            if (
                resumable_execution is not None
                and resumable_execution.checkpoint is not None
                and resumable_execution.callback_receipt is not None
                and not resumable_execution.resume_dispatch_pending
                and app.accepted_run_executor is not None
            ):
                resumable_execution.resume_dispatch_pending = True
                try:
                    resume_future = app._submit_accepted_run(
                        submission.run_id,
                        done_callback=app._accepted_run_resume_done_callback(
                            submission.run_id
                        ),
                    )
                    resumable_execution.resume_future = resume_future
                except Exception as error:
                    resumable_execution.resume_dispatch_pending = False
                    correlation_id = app._record_server_error(
                        method=request.method,
                        route=route.path,
                        operation=route.operation,
                        status_code=503,
                        error_code="server.callback.resume_executor_unavailable",
                        error=error,
                    )
                    paused_record = _freeze_json_value(
                        "run control record",
                        "record",
                        {
                            "operation": "resume_run",
                            "status": "paused_callback_delivery",
                            "reason": "Accepted-run callback resume is unavailable.",
                            "errorCode": "server.callback.resume_executor_unavailable",
                            "correlationId": correlation_id,
                            "occurredAt": submission.received_at,
                            "lastCursor": (
                                f"{submission.run_id}:"
                                f"{app._last_event_sequence(app._events_by_run_id[submission.run_id])}"
                            ),
                        },
                    )
                    assert isinstance(paused_record, Mapping)
                    # The submission is already committed above; a 429
                    # here would tell the client to retry a callback the
                    # server has recorded. Report acceptance and only
                    # skip the run-control record when capacity is gone.
                    capacity_exhausted = (
                        app._run_control_capacity_response(
                            submission.run_id,
                            paused_record,
                        )
                        is not None
                    )
                    if not capacity_exhausted:
                        app._record_run_control(
                            submission.run_id,
                            paused_record,
                        )
                    app._accepted_run_condition.notify_all()
                    paused_payload = submission.response_payload()
                    paused_payload["status"] = "paused_callback_delivery"
                    if capacity_exhausted:
                        paused_payload["resumeDeferred"] = True
                    return ServerResponse.json(202, paused_payload)
                app._accepted_run_condition.notify_all()
            elif (
                resumable_execution is not None
                and resumable_execution.checkpoint is not None
                and resumable_execution.callback_receipt is not None
                and app.accepted_run_executor is None
            ):
                paused_record = _freeze_json_value(
                    "run control record",
                    "record",
                    {
                        "operation": "resume_run",
                        "status": "paused_callback_delivery",
                        "reason": "accepted run callback resume executor is unavailable",
                        "occurredAt": submission.received_at,
                        "lastCursor": (
                            f"{submission.run_id}:"
                            f"{app._last_event_sequence(app._events_by_run_id[submission.run_id])}"
                        ),
                    },
                )
                assert isinstance(paused_record, Mapping)
                capacity_exhausted = (
                    app._run_control_capacity_response(
                        submission.run_id,
                        paused_record,
                    )
                    is not None
                )
                if not capacity_exhausted:
                    app._record_run_control(
                        submission.run_id,
                        paused_record,
                    )
                app._accepted_run_condition.notify_all()
                paused_payload = submission.response_payload()
                paused_payload["status"] = "paused_callback_delivery"
                if capacity_exhausted:
                    paused_payload["resumeDeferred"] = True
                return ServerResponse.json(202, paused_payload)
            return ServerResponse.json(202, submission.response_payload())
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            if str(error) == "server async callback operation_id must match callback endpoint operation_id":
                try:
                    body = _server_request_json_body(request, "server async callback")
                    if not isinstance(body, Mapping):
                        raise ValueError("server async callback body must be a JSON object")
                    payload = body.get("payload")
                    if payload is None:
                        raise ValueError("server async callback payload is required")
                    idempotency_key = _callback_idempotency_key(body, request.headers)
                    submission = ServerAsyncCallbackSubmission(
                        operation_id=route_match.path_params.get("operation_id", ""),
                        callback_id=_validate_exact_non_empty_string(
                            "server async callback",
                            "callback_id",
                            _callback_alias_value(body, "callback_id", "callbackId", ""),
                        ),
                        idempotency_key=_validate_exact_non_empty_string(
                            "server async callback",
                            "idempotency_key",
                            idempotency_key,
                        ),
                        payload=payload,
                        payload_digest=_optional_callback_string(body, "payload_digest", "payloadDigest") or "",
                        run_id=_optional_callback_string(body, "run_id", "runId"),
                        node_id=_optional_callback_string(body, "node_id", "nodeId"),
                        attempt_id=_optional_callback_string(body, "attempt_id", "attemptId"),
                        provider_operation_id=_optional_callback_string(
                            body,
                            "provider_operation_id",
                            "providerOperationId",
                        ),
                        artifacts=body.get("artifacts", ()),
                        received_at=request.requested_at or _utc_now_iso(),
                        verified_by=(
                            auth_decision.principal.principal_id
                            if auth_decision.principal is not None
                            else "unauthenticated"
                        ),
                        policy_snapshot_id=_validate_exact_non_empty_string(
                            "server async callback",
                            "policy_snapshot_id",
                            _callback_alias_value(body, "policy_snapshot_id", "policySnapshotId", "local"),
                        ),
                    )
                    rejection = ServerAsyncCallbackRejection.operation_id_mismatch(submission)
                    app._async_callback_rejections_by_operation_id.append(
                        rejection
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return app._safe_error_response(
                request,
                route_match,
                status_code=400,
                error_code="server.callback.invalid_request",
                message="The callback request is invalid.",
                error=error,
            )
        finally:
            if callback_state_locked:
                app._accepted_run_condition.release()
    return ServerResponse.json(
        501,
        {
            "ok": False,
            "error": (
                f"server operation {route.operation!r} is not implemented"
            ),
        },
    )
