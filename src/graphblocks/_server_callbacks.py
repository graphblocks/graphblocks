"""Server async-callback and event-subscription contracts.

Extracted verbatim from ``graphblocks.server`` (which re-imports every name,
so the ``graphblocks.server`` facade is unchanged): async callback
submissions/rejections and their bounded rejection history, event
subscriptions, callback registrations, delivery results, the delivery hook
protocol, and the alias/idempotency-key helpers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from ._canonical_reference import canonical_hash
from ._server_validation import (
    _canonical_json_size_bytes,
    _freeze_json_value,
    _principal_response_payload,
    _server_alias_value,
    _server_request_json_body,
    _thaw_json_value,
    _utc_now_iso,
    _validate_callback_delivery_target,
    _validate_callback_failure_policy,
    _validate_callback_not_authoritative,
    _validate_callback_subscription_scope,
    _validate_callback_subscription_status,
    _validate_exact_non_empty_string,
    _validate_iso_datetime,
    _validate_mandatory_callback_policy,
    _validate_server_event_filter,
    DEFAULT_MAX_ASYNC_CALLBACK_REJECTION_HISTORY_BYTES,
    MAX_RUN_CURSOR_SEQUENCE,
    MAX_SERVER_CALLBACK_DELIVERY_ERROR_BYTES,
    MAX_SERVER_CALLBACK_DELIVERY_IDENTIFIER_BYTES,
    MAX_SERVER_CALLBACK_DELIVERY_TIMESTAMP_BYTES,
    VALID_SERVER_CALLBACK_DELIVERY_STATUSES,
)
from .policy import PrincipalRef

if TYPE_CHECKING:
    from ._server_transport import ServerRequest

@dataclass(frozen=True, slots=True)
class ServerAsyncCallbackSubmission:
    operation_id: str
    callback_id: str
    idempotency_key: str
    payload: Mapping[str, object]
    payload_digest: str = ""
    run_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    provider_operation_id: str | None = None
    artifacts: tuple[object, ...] = field(default_factory=tuple)
    received_at: str = ""
    verified_by: str = "unauthenticated"
    policy_snapshot_id: str = "local"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _validate_exact_non_empty_string("server async callback", "operation_id", self.operation_id),
        )
        object.__setattr__(
            self,
            "callback_id",
            _validate_exact_non_empty_string("server async callback", "callback_id", self.callback_id),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_exact_non_empty_string("server async callback", "idempotency_key", self.idempotency_key),
        )
        if not isinstance(self.payload, Mapping):
            raise ValueError("server async callback payload must be a JSON object")
        object.__setattr__(
            self,
            "payload",
            _freeze_json_value("server async callback", "payload", self.payload),
        )
        if isinstance(self.artifacts, (str, bytes, bytearray, memoryview)) or isinstance(self.artifacts, Mapping):
            raise ValueError("server async callback artifacts must be a sequence")
        try:
            artifacts = []
            for artifact in self.artifacts:
                artifact_value = artifact
                if isinstance(artifact, Mapping):
                    artifact_value = dict(artifact)
                    for source_key, target_key in (
                        ("artifactId", "artifact_id"),
                        ("mediaType", "media_type"),
                        ("sizeBytes", "size_bytes"),
                    ):
                        if source_key in artifact_value:
                            if target_key in artifact_value and artifact_value[target_key] != artifact_value[source_key]:
                                raise ValueError("server async callback artifacts must not mix conflicting field aliases")
                            artifact_value[target_key] = artifact_value.pop(source_key)
                artifacts.append(_freeze_json_value("server async callback", "artifacts", artifact_value))
            artifacts = tuple(artifacts)
        except TypeError:
            raise ValueError("server async callback artifacts must be a sequence") from None
        artifact_ids: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise ValueError("server async callback artifacts entries must be JSON objects")
            artifact_id = artifact.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("server async callback artifacts artifact_id must be a non-empty string")
            if artifact_id != artifact_id.strip():
                raise ValueError("server async callback artifacts artifact_id must not contain surrounding whitespace")
            uri = artifact.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                raise ValueError("server async callback artifacts uri must be a non-empty string")
            if uri != uri.strip():
                raise ValueError("server async callback artifacts uri must not contain surrounding whitespace")
            for field_name in ("media_type", "checksum"):
                value = artifact.get(field_name)
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"server async callback artifacts {field_name} must be a non-empty string")
                    if value != value.strip():
                        raise ValueError(
                            f"server async callback artifacts {field_name} must not contain surrounding whitespace"
                        )
            size_bytes = artifact.get("size_bytes")
            if size_bytes is not None and (
                isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
            ):
                raise ValueError("server async callback artifacts size_bytes must be a non-negative integer")
            if artifact_id in artifact_ids:
                raise ValueError("server async callback artifacts must not contain duplicate artifact_id")
            artifact_ids.add(artifact_id)
        object.__setattr__(self, "artifacts", artifacts)
        if self.payload_digest == "":
            object.__setattr__(
                self,
                "payload_digest",
                canonical_hash(_thaw_json_value(self.payload)),
            )
        else:
            payload_digest = _validate_exact_non_empty_string(
                "server async callback",
                "payload_digest",
                self.payload_digest,
            )
            if payload_digest != canonical_hash(_thaw_json_value(self.payload)):
                raise ValueError("server async callback payload_digest must match payload")
            object.__setattr__(self, "payload_digest", payload_digest)
        for field_name in ("run_id", "node_id", "attempt_id", "provider_operation_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _validate_exact_non_empty_string("server async callback", field_name, value),
                )
        if self.received_at != "":
            object.__setattr__(
                self,
                "received_at",
                _validate_iso_datetime("server async callback", "received_at", self.received_at),
            )
        object.__setattr__(
            self,
            "verified_by",
            _validate_exact_non_empty_string("server async callback", "verified_by", self.verified_by),
        )
        object.__setattr__(
            self,
            "policy_snapshot_id",
            _validate_exact_non_empty_string(
                "server async callback",
                "policy_snapshot_id",
                self.policy_snapshot_id,
            ),
        )

    @classmethod
    def from_request(
        cls,
        *,
        operation_id: str,
        request: ServerRequest,
        verified_by: str = "unauthenticated",
    ) -> ServerAsyncCallbackSubmission:
        body = _server_request_json_body(request, "server async callback")
        if not isinstance(body, Mapping):
            raise ValueError("server async callback body must be a JSON object")
        declared_operation_id = _callback_alias_value(body, "operation_id", "operationId")
        if declared_operation_id is not None:
            declared_operation_id = _validate_exact_non_empty_string(
                "server async callback",
                "operation_id",
                declared_operation_id,
            )
            endpoint_operation_id = _validate_exact_non_empty_string(
                "server async callback",
                "operation_id",
                operation_id,
            )
            if declared_operation_id != endpoint_operation_id:
                raise ValueError("server async callback operation_id must match callback endpoint operation_id")
        idempotency_key = _callback_idempotency_key(body, request.headers)
        payload = body.get("payload")
        if payload is None:
            raise ValueError("server async callback payload is required")
        return cls(
            operation_id=operation_id,
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
            verified_by=verified_by,
            policy_snapshot_id=_validate_exact_non_empty_string(
                "server async callback",
                "policy_snapshot_id",
                _callback_alias_value(body, "policy_snapshot_id", "policySnapshotId", "local"),
            ),
        )

    def response_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "operationId": self.operation_id,
            "callbackId": self.callback_id,
            "idempotencyKey": self.idempotency_key,
            "payloadDigest": self.payload_digest,
            "verifiedBy": self.verified_by,
            "policySnapshotId": self.policy_snapshot_id,
            "status": "accepted",
        }
        if self.artifacts:
            payload["artifacts"] = [_thaw_json_value(artifact) for artifact in self.artifacts]
        if self.run_id is not None:
            payload["runId"] = self.run_id
        if self.node_id is not None:
            payload["nodeId"] = self.node_id
        if self.attempt_id is not None:
            payload["attemptId"] = self.attempt_id
        if self.provider_operation_id is not None:
            payload["providerOperationId"] = self.provider_operation_id
        return payload

    def _retained_size_bytes(self) -> int:
        retained_value = self.response_payload()
        retained_value["payload"] = _thaw_json_value(self.payload)
        retained_value["receivedAt"] = self.received_at
        return _canonical_json_size_bytes(retained_value)

    def duplicate_response_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "operationId": self.operation_id,
            "callbackId": self.callback_id,
            "idempotencyKey": self.idempotency_key,
            "payloadDigest": self.payload_digest,
            "verifiedBy": self.verified_by,
            "policySnapshotId": self.policy_snapshot_id,
            "status": "duplicate",
            "duplicate": True,
        }
        if self.artifacts:
            payload["artifacts"] = [_thaw_json_value(artifact) for artifact in self.artifacts]
        if self.run_id is not None:
            payload["runId"] = self.run_id
        if self.node_id is not None:
            payload["nodeId"] = self.node_id
        if self.attempt_id is not None:
            payload["attemptId"] = self.attempt_id
        if self.provider_operation_id is not None:
            payload["providerOperationId"] = self.provider_operation_id
        return payload


@dataclass(frozen=True, slots=True)
class ServerAsyncCallbackRejection:
    operation_id: str
    callback_id: str
    idempotency_key: str
    reason: str
    received_at: str
    payload_digest: str = ""
    verified_by: str = "unauthenticated"
    policy_snapshot_id: str = "local"
    run_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    provider_operation_id: str | None = None
    status: str | None = None
    artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "callback_id", "idempotency_key", "reason"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_non_empty_string(
                    "server async callback rejection",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "received_at",
            _validate_iso_datetime("server async callback rejection", "received_at", self.received_at),
        )
        for field_name in ("payload_digest", "verified_by", "policy_snapshot_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_non_empty_string(
                    "server async callback rejection",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        for field_name in ("run_id", "node_id", "attempt_id", "provider_operation_id", "status"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _validate_exact_non_empty_string("server async callback rejection", field_name, value),
                )
        object.__setattr__(
            self,
            "artifact_ids",
            tuple(
                _validate_exact_non_empty_string("server async callback rejection", "artifact_ids", artifact_id)
                for artifact_id in self.artifact_ids
            ),
        )

    @staticmethod
    def _receipt_metadata(submission: ServerAsyncCallbackSubmission) -> dict[str, object]:
        return {
            "payload_digest": submission.payload_digest,
            "verified_by": submission.verified_by,
            "policy_snapshot_id": submission.policy_snapshot_id,
            "provider_operation_id": submission.provider_operation_id,
            "artifact_ids": tuple(
                artifact["artifact_id"]
                for artifact in submission.artifacts
                if isinstance(artifact, Mapping) and isinstance(artifact.get("artifact_id"), str)
            ),
        }

    @classmethod
    def terminal_run(
        cls,
        submission: ServerAsyncCallbackSubmission,
        status: object,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            status=_validate_exact_non_empty_string("server async callback rejection", "status", status),
            reason="terminal_run",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def unknown_run(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="unknown_run",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def authentication_failed(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="authentication_failed",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def operation_id_mismatch(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="operation_id_mismatch",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def payload_too_large(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="payload_too_large",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def missing_attempt_fence(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            reason="missing_attempt_fence",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def missing_node_fence(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            attempt_id=submission.attempt_id,
            reason="missing_node_fence",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def stale_attempt(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="stale_attempt",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def idempotency_conflict(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="idempotency_conflict",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def node_mismatch(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="node_mismatch",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def provider_operation_mismatch(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="provider_operation_mismatch",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def scope_mismatch(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="scope_mismatch",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    @classmethod
    def duplicate_operation_receipt(
        cls,
        submission: ServerAsyncCallbackSubmission,
    ) -> ServerAsyncCallbackRejection:
        return cls(
            operation_id=submission.operation_id,
            callback_id=submission.callback_id,
            idempotency_key=submission.idempotency_key,
            run_id=submission.run_id,
            node_id=submission.node_id,
            attempt_id=submission.attempt_id,
            reason="duplicate_operation_receipt",
            received_at=submission.received_at,
            **cls._receipt_metadata(submission),
        )

    def protocol_value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "operationId": self.operation_id,
            "callbackId": self.callback_id,
            "idempotencyKey": self.idempotency_key,
            "payloadDigest": self.payload_digest,
            "verifiedBy": self.verified_by,
            "policySnapshotId": self.policy_snapshot_id,
            "reason": self.reason,
            "receivedAt": self.received_at,
        }
        if self.run_id is not None:
            value["runId"] = self.run_id
        if self.node_id is not None:
            value["nodeId"] = self.node_id
        if self.attempt_id is not None:
            value["attemptId"] = self.attempt_id
        if self.provider_operation_id is not None:
            value["providerOperationId"] = self.provider_operation_id
        if self.status is not None:
            value["status"] = self.status
        if self.artifact_ids:
            value["artifactIds"] = list(self.artifact_ids)
        return value


class _BoundedAsyncCallbackRejectionHistory(
    dict[str, list[ServerAsyncCallbackRejection]]
):
    """Keep diagnostic callback receipts useful without accepting unbounded input."""

    _MAX_OPERATION_IDS = 1_024
    _MAX_REJECTIONS_PER_OPERATION = 32
    _MISSING = object()

    def __init__(
        self,
        max_retained_bytes: int = (
            DEFAULT_MAX_ASYNC_CALLBACK_REJECTION_HISTORY_BYTES
        ),
    ) -> None:
        super().__init__()
        if (
            isinstance(max_retained_bytes, bool)
            or not isinstance(max_retained_bytes, int)
            or max_retained_bytes < 1
        ):
            raise ValueError(
                "server max_async_callback_rejection_history_bytes "
                "must be a positive integer"
            )
        self._max_retained_bytes = max_retained_bytes
        self._retained_bytes_by_operation_id: dict[str, int] = {}
        self._retained_bytes = 0
        self._lock = Lock()

    @staticmethod
    def _history_size_bytes(
        operation_id: str,
        rejections: list[ServerAsyncCallbackRejection]
        | tuple[ServerAsyncCallbackRejection, ...],
    ) -> int:
        return _canonical_json_size_bytes(
            {
                "operationId": operation_id,
                "rejections": [
                    rejection.protocol_value()
                    for rejection in rejections
                ],
            }
        )

    def _newest_fitting_history(
        self,
        operation_id: str,
        rejections: list[ServerAsyncCallbackRejection]
        | tuple[ServerAsyncCallbackRejection, ...],
    ) -> tuple[
        list[ServerAsyncCallbackRejection],
        int,
    ] | None:
        if len(operation_id) > self._max_retained_bytes:
            return None
        if len(rejections) <= self._MAX_REJECTIONS_PER_OPERATION:
            retained_bytes = self._history_size_bytes(
                operation_id,
                rejections,
            )
            if retained_bytes <= self._max_retained_bytes:
                return (
                    rejections
                    if type(rejections) is list
                    else list(rejections),
                    retained_bytes,
                )
        bounded_rejections = list(
            rejections[-self._MAX_REJECTIONS_PER_OPERATION :]
        )
        for start_index in range(len(bounded_rejections)):
            candidate = bounded_rejections[start_index:]
            candidate_bytes = self._history_size_bytes(
                operation_id,
                candidate,
            )
            if candidate_bytes <= self._max_retained_bytes:
                return candidate, candidate_bytes
        return None

    def _remove_locked(
        self,
        operation_id: str,
    ) -> list[ServerAsyncCallbackRejection]:
        value = super().pop(operation_id)
        self._retained_bytes -= (
            self._retained_bytes_by_operation_id.pop(operation_id)
        )
        return value

    def _evict_oldest_other_locked(
        self,
        operation_id: str,
    ) -> bool:
        for retained_operation_id in tuple(super().keys()):
            if retained_operation_id != operation_id:
                self._remove_locked(retained_operation_id)
                return True
        return False

    def _store_locked(
        self,
        operation_id: str,
        retained_rejections: list[ServerAsyncCallbackRejection],
        retained_bytes: int,
    ) -> None:
        existing_bytes = self._retained_bytes_by_operation_id.get(
            operation_id,
            0,
        )
        if (
            operation_id not in self
            and len(self) >= self._MAX_OPERATION_IDS
        ):
            self._evict_oldest_other_locked(operation_id)
        while (
            self._retained_bytes
            - existing_bytes
            + retained_bytes
            > self._max_retained_bytes
        ):
            if not self._evict_oldest_other_locked(operation_id):
                return
        super().__setitem__(operation_id, retained_rejections)
        self._retained_bytes_by_operation_id[
            operation_id
        ] = retained_bytes
        self._retained_bytes = (
            self._retained_bytes
            - existing_bytes
            + retained_bytes
        )

    def __setitem__(
        self,
        operation_id: str,
        rejections: list[ServerAsyncCallbackRejection]
        | tuple[ServerAsyncCallbackRejection, ...],
    ) -> None:
        if not rejections:
            self.pop(operation_id, None)
            return
        fitting_history = self._newest_fitting_history(
            operation_id,
            rejections,
        )
        if fitting_history is None:
            return
        retained_rejections, retained_bytes = fitting_history
        with self._lock:
            self._store_locked(
                operation_id,
                retained_rejections,
                retained_bytes,
            )

    def append(self, rejection: ServerAsyncCallbackRejection) -> None:
        operation_id = rejection.operation_id
        with self._lock:
            existing = super().get(operation_id)
            candidate = existing if existing is not None else []
            candidate.append(rejection)
            fitting_history = self._newest_fitting_history(
                operation_id,
                candidate,
            )
            if fitting_history is None:
                if existing is not None:
                    existing.pop()
                return
            retained_rejections, retained_bytes = fitting_history
            self._store_locked(
                operation_id,
                retained_rejections,
                retained_bytes,
            )

    def __delitem__(self, operation_id: str) -> None:
        with self._lock:
            self._remove_locked(operation_id)

    def pop(
        self,
        operation_id: str,
        default: object = _MISSING,
    ) -> list[ServerAsyncCallbackRejection] | object:
        with self._lock:
            if operation_id in self:
                return self._remove_locked(operation_id)
            if default is self._MISSING:
                raise KeyError(operation_id)
            return default

    def clear(self) -> None:
        with self._lock:
            super().clear()
            self._retained_bytes_by_operation_id.clear()
            self._retained_bytes = 0

    def remove_run(self, run_id: str) -> None:
        with self._lock:
            for operation_id, rejections in tuple(super().items()):
                retained_rejections = [
                    rejection
                    for rejection in rejections
                    if rejection.run_id != run_id
                ]
                if not retained_rejections:
                    self._remove_locked(operation_id)
                    continue
                retained_bytes = self._history_size_bytes(
                    operation_id,
                    retained_rejections,
                )
                existing_bytes = (
                    self._retained_bytes_by_operation_id[operation_id]
                )
                super().__setitem__(
                    operation_id,
                    retained_rejections,
                )
                self._retained_bytes_by_operation_id[
                    operation_id
                ] = retained_bytes
                self._retained_bytes = (
                    self._retained_bytes
                    - existing_bytes
                    + retained_bytes
                )

    def snapshot(
        self,
        operation_id: str,
    ) -> tuple[ServerAsyncCallbackRejection, ...]:
        with self._lock:
            return tuple(super().get(operation_id, []))

    @property
    def retained_size_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes


@dataclass(frozen=True, slots=True)
class ServerEventSubscription:
    subscription_id: str
    run_id: str
    event_filter: Mapping[str, object]
    delivery: Mapping[str, object]
    status: str = "active"
    failure_policy: str = "retry_then_dead_letter"
    replay_from_cursor: str | None = None
    created_at: str = ""
    owner: PrincipalRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _validate_exact_non_empty_string("server event subscription", "subscription_id", self.subscription_id),
        )
        object.__setattr__(
            self,
            "run_id",
            _validate_exact_non_empty_string("server event subscription", "run_id", self.run_id),
        )
        if not isinstance(self.event_filter, Mapping):
            raise ValueError("server event subscription event_filter must be a mapping")
        if not isinstance(self.delivery, Mapping):
            raise ValueError("server event subscription delivery must be a mapping")
        event_filter = _freeze_json_value(
            "server event subscription",
            "event_filter",
            self.event_filter,
        )
        delivery = _freeze_json_value("server event subscription", "delivery", self.delivery)
        assert isinstance(event_filter, Mapping)
        assert isinstance(delivery, Mapping)
        _validate_server_event_filter("server event subscription", event_filter)
        _validate_callback_delivery_target("server event subscription", delivery)
        object.__setattr__(self, "event_filter", event_filter)
        object.__setattr__(self, "delivery", delivery)
        object.__setattr__(
            self,
            "status",
            _validate_callback_subscription_status("server event subscription", self.status),
        )
        object.__setattr__(
            self,
            "failure_policy",
            _validate_callback_failure_policy(self.failure_policy),
        )
        if self.replay_from_cursor is not None:
            object.__setattr__(
                self,
                "replay_from_cursor",
                _validate_exact_non_empty_string(
                    "server event subscription",
                    "replay_from_cursor",
                    self.replay_from_cursor,
                ),
            )
        if self.created_at != "":
            object.__setattr__(
                self,
                "created_at",
                _validate_iso_datetime("server event subscription", "created_at", self.created_at),
            )
        if self.owner is not None and not isinstance(self.owner, PrincipalRef):
            raise ValueError("server event subscription owner must be a PrincipalRef")

    @classmethod
    def from_request(
        cls,
        *,
        run_id: str,
        request: ServerRequest,
        ordinal: int,
        owner: PrincipalRef | None = None,
    ) -> ServerEventSubscription:
        body = _server_request_json_body(request, "subscribe request")
        if not isinstance(body, Mapping):
            raise ValueError("subscribe request body must be a JSON object")
        event_filter = _server_alias_value(
            body,
            "subscribe request",
            "event_filter",
            "eventFilter",
            {},
        )
        delivery = body.get("delivery", {})
        if not isinstance(event_filter, Mapping):
            raise ValueError("subscribe request event_filter must be a JSON object")
        if not isinstance(delivery, Mapping):
            raise ValueError("subscribe request delivery must be a JSON object")
        subscription_id = _server_alias_value(
            body,
            "subscribe request",
            "subscription_id",
            "subscriptionId",
        )
        if subscription_id is None:
            subscription_id = f"sub-{run_id}-{ordinal:06d}"
        replay_from_cursor = _server_alias_value(
            body,
            "subscribe request",
            "replay_from_cursor",
            "replayFromCursor",
        )
        failure_policy = _validate_callback_failure_policy(
            _server_alias_value(
                body,
                "subscribe request",
                "failure_policy",
                "failurePolicy",
                "retry_then_dead_letter",
            )
        )
        _validate_callback_not_authoritative("server event subscription", body)
        _validate_mandatory_callback_policy("server event subscription", body, delivery, failure_policy)
        return cls(
            subscription_id=_validate_exact_non_empty_string(
                "server event subscription",
                "subscription_id",
                subscription_id,
            ),
            run_id=run_id,
            event_filter=event_filter,
            delivery=delivery,
            failure_policy=failure_policy,
            replay_from_cursor=(
                _validate_exact_non_empty_string(
                    "server event subscription",
                    "replay_from_cursor",
                    replay_from_cursor,
                )
                if replay_from_cursor is not None
                else None
            ),
            created_at=request.requested_at or _utc_now_iso(),
            owner=owner,
        )

    def response_payload(self, replayed_events: list[dict[str, object]], last_cursor: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "subscriptionId": self.subscription_id,
            "runId": self.run_id,
            "status": self.status,
            "failurePolicy": self.failure_policy,
            "replayFromCursor": self.replay_from_cursor,
            "lastCursor": last_cursor,
            "delivery": _thaw_json_value(self.delivery),
            "eventFilter": _thaw_json_value(self.event_filter),
            "events": replayed_events,
        }
        if self.owner is not None:
            payload["owner"] = _principal_response_payload(self.owner)
        return payload


@dataclass(frozen=True, slots=True)
class ServerCallbackRegistration:
    subscription_id: str
    scope: str
    scope_id: str
    event_filter: Mapping[str, object]
    delivery: Mapping[str, object]
    status: str = "active"
    failure_policy: str = "retry_then_dead_letter"
    replay_from_cursor: str | None = None
    created_at: str = ""
    owner: PrincipalRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _validate_exact_non_empty_string("server callback registration", "subscription_id", self.subscription_id),
        )
        object.__setattr__(
            self,
            "scope",
            _validate_callback_subscription_scope(self.scope),
        )
        object.__setattr__(
            self,
            "scope_id",
            _validate_exact_non_empty_string("server callback registration", "scope_id", self.scope_id),
        )
        if not isinstance(self.event_filter, Mapping):
            raise ValueError("server callback registration event_filter must be a mapping")
        if not isinstance(self.delivery, Mapping):
            raise ValueError("server callback registration delivery must be a mapping")
        event_filter = _freeze_json_value(
            "server callback registration",
            "event_filter",
            self.event_filter,
        )
        delivery = _freeze_json_value("server callback registration", "delivery", self.delivery)
        assert isinstance(event_filter, Mapping)
        assert isinstance(delivery, Mapping)
        _validate_server_event_filter("server event subscription", event_filter)
        _validate_callback_delivery_target("server callback registration", delivery)
        object.__setattr__(self, "event_filter", event_filter)
        object.__setattr__(self, "delivery", delivery)
        object.__setattr__(
            self,
            "status",
            _validate_callback_subscription_status("server callback registration", self.status),
        )
        object.__setattr__(
            self,
            "failure_policy",
            _validate_callback_failure_policy(self.failure_policy),
        )
        if self.replay_from_cursor is not None:
            object.__setattr__(
                self,
                "replay_from_cursor",
                _validate_exact_non_empty_string(
                    "server callback registration",
                    "replay_from_cursor",
                    self.replay_from_cursor,
                ),
            )
        if self.created_at != "":
            object.__setattr__(
                self,
                "created_at",
                _validate_iso_datetime("server callback registration", "created_at", self.created_at),
            )
        if self.owner is not None and not isinstance(self.owner, PrincipalRef):
            raise ValueError("server callback registration owner must be a PrincipalRef")

    @classmethod
    def from_request(
        cls,
        *,
        request: ServerRequest,
        ordinal: int,
        owner: PrincipalRef | None = None,
    ) -> ServerCallbackRegistration:
        body = _server_request_json_body(request, "register callback request")
        if not isinstance(body, Mapping):
            raise ValueError("register callback request body must be a JSON object")
        event_filter = _server_alias_value(
            body,
            "register callback request",
            "event_filter",
            "eventFilter",
            {},
        )
        delivery = body.get("delivery", {})
        if not isinstance(event_filter, Mapping):
            raise ValueError("register callback request event_filter must be a JSON object")
        if not isinstance(delivery, Mapping):
            raise ValueError("register callback request delivery must be a JSON object")
        subscription_id = _server_alias_value(
            body,
            "register callback request",
            "subscription_id",
            "subscriptionId",
        )
        if subscription_id is None:
            subscription_id = f"callback-sub-{ordinal:06d}"
        replay_from_cursor = _server_alias_value(
            body,
            "register callback request",
            "replay_from_cursor",
            "replayFromCursor",
        )
        failure_policy = _validate_callback_failure_policy(
            _server_alias_value(
                body,
                "register callback request",
                "failure_policy",
                "failurePolicy",
                "retry_then_dead_letter",
            )
        )
        _validate_callback_not_authoritative("server callback registration", body)
        _validate_mandatory_callback_policy("server callback registration", body, delivery, failure_policy)
        return cls(
            subscription_id=_validate_exact_non_empty_string(
                "server callback registration",
                "subscription_id",
                subscription_id,
            ),
            scope=_validate_callback_subscription_scope(body.get("scope", "")),
            scope_id=_validate_exact_non_empty_string(
                "server callback registration",
                "scope_id",
                _server_alias_value(
                    body,
                    "register callback request",
                    "scope_id",
                    "scopeId",
                    "",
                ),
            ),
            event_filter=event_filter,
            delivery=delivery,
            failure_policy=failure_policy,
            replay_from_cursor=(
                _validate_exact_non_empty_string(
                    "server callback registration",
                    "replay_from_cursor",
                    replay_from_cursor,
                )
                if replay_from_cursor is not None
                else None
            ),
            created_at=request.requested_at or _utc_now_iso(),
            owner=owner,
        )

    def response_payload(
        self,
        replayed_events: list[dict[str, object]],
        last_cursor: str | None,
        delivery_results: tuple[ServerCallbackDeliveryResult, ...] = (),
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "subscriptionId": self.subscription_id,
            "scope": self.scope,
            "scopeId": self.scope_id,
            "status": self.status,
            "failurePolicy": self.failure_policy,
            "replayFromCursor": self.replay_from_cursor,
            "lastCursor": last_cursor,
            "delivery": _thaw_json_value(self.delivery),
            "eventFilter": _thaw_json_value(self.event_filter),
            "events": replayed_events,
        }
        if delivery_results:
            payload["deliveries"] = [result.protocol_value() for result in delivery_results]
        if self.owner is not None:
            payload["owner"] = _principal_response_payload(self.owner)
        return payload

    def _retained_size_bytes(self) -> int:
        retained_value: dict[str, object] = {
            "subscriptionId": self.subscription_id,
            "scope": self.scope,
            "scopeId": self.scope_id,
            "eventFilter": _thaw_json_value(self.event_filter),
            "delivery": _thaw_json_value(self.delivery),
            "status": "revoked",
            "failurePolicy": self.failure_policy,
            "replayFromCursor": self.replay_from_cursor,
            "createdAt": self.created_at,
        }
        if self.owner is not None:
            retained_value["owner"] = _principal_response_payload(self.owner)
        return _canonical_json_size_bytes(retained_value)


@dataclass(frozen=True, slots=True)
class ServerCallbackDeliveryResult:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    sequence: int
    cursor: str
    attempt: int
    idempotency_key: str
    status: str
    state_version: int = field(default=1, kw_only=True)
    status_code: int | None = None
    delivered_at: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_id",
            "subscription_id",
            "event_id",
            "run_id",
            "cursor",
            "idempotency_key",
        ):
            field_value = _validate_exact_non_empty_string(
                "server callback delivery result",
                field_name,
                getattr(self, field_name),
            )
            if (
                len(field_value.encode("utf-8"))
                > MAX_SERVER_CALLBACK_DELIVERY_IDENTIFIER_BYTES
            ):
                raise ValueError(
                    f"server callback delivery result {field_name} must be at most "
                    f"{MAX_SERVER_CALLBACK_DELIVERY_IDENTIFIER_BYTES} UTF-8 bytes"
                )
            object.__setattr__(
                self,
                field_name,
                field_value,
            )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("server callback delivery result sequence must be a non-negative integer")
        if self.sequence > MAX_RUN_CURSOR_SEQUENCE:
            raise ValueError(
                "server callback delivery result sequence must be at most "
                f"{MAX_RUN_CURSOR_SEQUENCE}"
            )
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("server callback delivery result attempt must be a positive integer")
        if self.attempt > MAX_RUN_CURSOR_SEQUENCE:
            raise ValueError(
                "server callback delivery result attempt must be at most "
                f"{MAX_RUN_CURSOR_SEQUENCE}"
            )
        if (
            isinstance(self.state_version, bool)
            or not isinstance(self.state_version, int)
            or self.state_version < 1
        ):
            raise ValueError(
                "server callback delivery result state_version must be a positive integer"
            )
        if self.state_version > MAX_RUN_CURSOR_SEQUENCE:
            raise ValueError(
                "server callback delivery result state_version must be at most "
                f"{MAX_RUN_CURSOR_SEQUENCE}"
            )
        object.__setattr__(
            self,
            "status",
            _validate_exact_non_empty_string(
                "server callback delivery result",
                "status",
                self.status,
            ),
        )
        if self.status not in VALID_SERVER_CALLBACK_DELIVERY_STATUSES:
            raise ValueError(
                "server callback delivery result status must be one of "
                "pending, delivered, acknowledged, failed, dead_lettered, or cancelled"
            )
        if self.status_code is not None and (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or self.status_code < 100
            or self.status_code > 599
        ):
            raise ValueError("server callback delivery result status_code must be a valid HTTP status")
        if self.delivered_at is not None:
            delivered_at = _validate_exact_non_empty_string(
                "server callback delivery result",
                "delivered_at",
                self.delivered_at,
            )
            if (
                len(delivered_at.encode("utf-8"))
                > MAX_SERVER_CALLBACK_DELIVERY_TIMESTAMP_BYTES
            ):
                raise ValueError(
                    "server callback delivery result delivered_at must be at most "
                    f"{MAX_SERVER_CALLBACK_DELIVERY_TIMESTAMP_BYTES} UTF-8 bytes"
                )
            object.__setattr__(
                self,
                "delivered_at",
                _validate_iso_datetime(
                    "server callback delivery result",
                    "delivered_at",
                    delivered_at,
                ),
            )
        if self.last_error is not None:
            last_error = _validate_exact_non_empty_string(
                "server callback delivery result",
                "last_error",
                self.last_error,
            )
            if (
                len(last_error.encode("utf-8"))
                > MAX_SERVER_CALLBACK_DELIVERY_ERROR_BYTES
            ):
                raise ValueError(
                    "server callback delivery result last_error must be at most "
                    f"{MAX_SERVER_CALLBACK_DELIVERY_ERROR_BYTES} UTF-8 bytes"
                )
            object.__setattr__(
                self,
                "last_error",
                last_error,
            )
        if self.status in {"delivered", "acknowledged"}:
            if self.status_code is None or self.delivered_at is None:
                raise ValueError(
                    "successful server callback delivery result requires status_code and delivered_at"
                )
            if self.last_error is not None:
                raise ValueError("successful server callback delivery result must not have last_error")
        if self.status in {"failed", "dead_lettered", "cancelled"} and self.last_error is None:
            raise ValueError("failed server callback delivery result requires last_error")

    def protocol_value(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "deliveryId": self.delivery_id,
            "subscriptionId": self.subscription_id,
            "eventId": self.event_id,
            "runId": self.run_id,
            "sequence": self.sequence,
            "cursor": self.cursor,
            "attempt": self.attempt,
            "idempotencyKey": self.idempotency_key,
            "status": self.status,
            "stateVersion": self.state_version,
        }
        if self.status_code is not None:
            payload["statusCode"] = self.status_code
        if self.delivered_at is not None:
            payload["deliveredAt"] = self.delivered_at
        if self.last_error is not None:
            payload["lastError"] = self.last_error
        return payload


class ServerCallbackDeliveryHook(Protocol):
    def deliver(
        self,
        registration: ServerCallbackRegistration,
        event: Mapping[str, object],
    ) -> ServerCallbackDeliveryResult:
        ...


def _callback_alias_value(
    body: Mapping[str, object],
    snake: str,
    camel: str,
    default: object | None = None,
) -> object | None:
    snake_present = snake in body
    camel_present = camel in body
    if snake_present and camel_present and body[snake] != body[camel]:
        raise ValueError(f"server async callback {snake} aliases must not conflict")
    return body.get(snake, body.get(camel, default))


def _optional_callback_string(body: Mapping[str, object], snake: str, camel: str) -> str | None:
    value = _callback_alias_value(body, snake, camel)
    if value is None:
        return None
    return _validate_exact_non_empty_string("server async callback", snake, value)


def _server_idempotency_key(
    body: Mapping[str, object],
    headers: Mapping[str, str],
    *,
    owner: str,
) -> object:
    graphblocks_header_value = headers.get("graphblocks-idempotency-key")
    legacy_header_value = headers.get("idempotency-key")
    if (
        graphblocks_header_value is not None
        and legacy_header_value is not None
        and graphblocks_header_value != legacy_header_value
    ):
        raise ValueError(f"{owner} idempotency_key header values must not conflict")
    header_value = graphblocks_header_value if graphblocks_header_value is not None else legacy_header_value
    body_value = _server_alias_value(
        body,
        owner,
        "idempotency_key",
        "idempotencyKey",
    )
    if body_value is not None and header_value is not None and body_value != header_value:
        raise ValueError(f"{owner} idempotency_key body/header values must not conflict")
    if body_value is not None:
        return body_value
    if header_value is not None:
        return header_value
    return ""


def _callback_idempotency_key(body: Mapping[str, object], headers: Mapping[str, str]) -> object:
    return _server_idempotency_key(
        body,
        headers,
        owner="server async callback",
    )
