"""Server auth, audit, run-record, and health contracts.

Extracted verbatim from ``graphblocks.server`` (which re-imports every name,
so the ``graphblocks.server`` facade is unchanged): auth/error audit events,
authentication and authorization decisions and hook protocols, the static
bearer hook, run authorization/retirement records, server health, and the
accepted-run execution holders.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from hmac import compare_digest
from types import MappingProxyType
from typing import Literal, Protocol

from ._canonical_reference import canonical_hash
from ._server_callbacks import ServerAsyncCallbackSubmission
from ._server_transport import ServerAuthRequest, ServerEndpoint
from ._server_validation import (
    _thaw_json_value,
    _validate_exact_non_empty_string,
    _validate_iso_datetime,
    _validate_non_empty_string,
    _validate_route_path,
    _validate_run_cursor,
    _validate_string_mapping,
    MAX_SERVER_AUTH_AUDIT_FAILURE_TYPE_BYTES,
    MAX_SERVER_AUTH_AUDIT_REQUEST_ID_BYTES,
    MAX_SERVER_ERROR_CODE_BYTES,
    MAX_SERVER_ERROR_CORRELATION_ID_BYTES,
    MAX_SERVER_ERROR_FAILURE_DETAIL_BYTES,
    SERVER_TERMINAL_RUN_STATES,
    ServerHealthStatus,
    VALID_SERVER_HEALTH_STATUSES,
)
from .policy import PrincipalRef, ResourceRef
from .runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
    RuntimeCheckpoint,
)

@dataclass(frozen=True, slots=True)
class ServerAuthAuditEvent:
    event_kind: Literal[
        "auth.hook_error",
        "auth.invalid_decision",
        "authz.hook_error",
        "authz.invalid_decision",
    ]
    method: str
    route: str
    operation: str
    failure_type: str
    credential_present: bool
    observed_at: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        owner = "server auth audit event"
        if self.event_kind not in {
            "auth.hook_error",
            "auth.invalid_decision",
            "authz.hook_error",
            "authz.invalid_decision",
        }:
            raise ValueError(f"{owner} event_kind is invalid")
        object.__setattr__(
            self,
            "method",
            _validate_exact_non_empty_string(
                owner,
                "method",
                self.method,
            ).upper(),
        )
        object.__setattr__(
            self,
            "route",
            _validate_route_path(owner, self.route),
        )
        object.__setattr__(
            self,
            "operation",
            _validate_exact_non_empty_string(
                owner,
                "operation",
                self.operation,
            ),
        )
        failure_type = _validate_exact_non_empty_string(
            owner,
            "failure_type",
            self.failure_type,
        )
        if (
            len(failure_type.encode("utf-8"))
            > MAX_SERVER_AUTH_AUDIT_FAILURE_TYPE_BYTES
        ):
            raise ValueError(f"{owner} failure_type exceeds byte limit")
        object.__setattr__(self, "failure_type", failure_type)
        if not isinstance(self.credential_present, bool):
            raise ValueError(
                f"{owner} credential_present must be a boolean"
            )
        object.__setattr__(
            self,
            "observed_at",
            _validate_iso_datetime(
                owner,
                "observed_at",
                self.observed_at,
            ),
        )
        if self.request_id is not None:
            request_id = _validate_exact_non_empty_string(
                owner,
                "request_id",
                self.request_id,
            )
            if (
                len(request_id.encode("utf-8"))
                > MAX_SERVER_AUTH_AUDIT_REQUEST_ID_BYTES
            ):
                raise ValueError(f"{owner} request_id exceeds byte limit")
            object.__setattr__(self, "request_id", request_id)


@dataclass(frozen=True, slots=True)
class ServerErrorAuditEvent:
    method: str
    route: str
    operation: str
    status_code: int
    error_code: str
    correlation_id: str
    failure_type: str
    failure_detail: str
    observed_at: str

    def __post_init__(self) -> None:
        owner = "server error audit event"
        object.__setattr__(
            self,
            "method",
            _validate_exact_non_empty_string(owner, "method", self.method).upper(),
        )
        object.__setattr__(self, "route", _validate_route_path(owner, self.route))
        object.__setattr__(
            self,
            "operation",
            _validate_exact_non_empty_string(owner, "operation", self.operation),
        )
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or self.status_code < 400
            or self.status_code > 599
        ):
            raise ValueError(f"{owner} status_code must be an HTTP error status")
        for field_name, value, max_bytes in (
            ("error_code", self.error_code, MAX_SERVER_ERROR_CODE_BYTES),
            (
                "correlation_id",
                self.correlation_id,
                MAX_SERVER_ERROR_CORRELATION_ID_BYTES,
            ),
            (
                "failure_type",
                self.failure_type,
                MAX_SERVER_AUTH_AUDIT_FAILURE_TYPE_BYTES,
            ),
            (
                "failure_detail",
                self.failure_detail,
                MAX_SERVER_ERROR_FAILURE_DETAIL_BYTES,
            ),
        ):
            normalized = _validate_exact_non_empty_string(owner, field_name, value)
            if len(normalized.encode("utf-8")) > max_bytes:
                raise ValueError(f"{owner} {field_name} exceeds byte limit")
            object.__setattr__(self, field_name, normalized)
        object.__setattr__(
            self,
            "observed_at",
            _validate_iso_datetime(owner, "observed_at", self.observed_at),
        )


@dataclass(frozen=True, slots=True)
class ServerAuthDecision:
    allowed: bool
    principal: PrincipalRef | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("server auth decision allowed must be a boolean")
        if self.principal is not None and not isinstance(self.principal, PrincipalRef):
            raise ValueError("server auth decision principal must be a PrincipalRef")
        if not isinstance(self.reason_codes, (list, tuple)):
            raise ValueError("server auth decision reason_codes must be a sequence")
        reason_codes = tuple(
            _validate_exact_non_empty_string(
                "server auth decision",
                "reason_codes",
                reason_code,
            )
            for reason_code in self.reason_codes
        )
        if len(set(reason_codes)) != len(reason_codes):
            raise ValueError("server auth decision reason_codes must not contain duplicates")
        object.__setattr__(self, "reason_codes", reason_codes)


@dataclass(frozen=True, slots=True)
class _ServerAuthenticationContext:
    decision: ServerAuthDecision
    audit_request_id: str | None


@dataclass(frozen=True, slots=True)
class ServerAuthorizationRequest:
    principal: PrincipalRef
    action: str
    route: ServerEndpoint
    path_params: Mapping[str, str]
    resource: ResourceRef | None
    tenant_id: str | None
    request_digest: str | None
    requested_at: str = ""

    def __post_init__(self) -> None:
        owner = "server authorization request"
        if not isinstance(self.principal, PrincipalRef):
            raise ValueError(f"{owner} principal must be a PrincipalRef")
        action = _validate_exact_non_empty_string(
            owner,
            "action",
            self.action,
        )
        if not isinstance(self.route, ServerEndpoint):
            raise ValueError(f"{owner} route must be a ServerEndpoint")
        if action != self.route.operation:
            raise ValueError(f"{owner} action must match route operation")
        object.__setattr__(self, "action", action)
        path_params = _validate_string_mapping(
            owner,
            "path_params",
            self.path_params,
        )
        object.__setattr__(self, "path_params", path_params)
        if self.resource is not None and not isinstance(
            self.resource,
            ResourceRef,
        ):
            raise ValueError(f"{owner} resource must be a ResourceRef or None")
        if self.tenant_id is not None:
            object.__setattr__(
                self,
                "tenant_id",
                _validate_exact_non_empty_string(
                    owner,
                    "tenant_id",
                    self.tenant_id,
                ),
            )
        if (
            self.resource is not None
            and self.resource.tenant_id is not None
            and self.tenant_id != self.resource.tenant_id
        ):
            raise ValueError(
                f"{owner} tenant_id must match resource tenant_id"
            )
        if self.request_digest is not None:
            request_digest = _validate_exact_non_empty_string(
                owner,
                "request_digest",
                self.request_digest,
            )
            if (
                len(request_digest) != 71
                or not request_digest.startswith("sha256:")
                or any(
                    character not in "0123456789abcdef"
                    for character in request_digest[7:]
                )
            ):
                raise ValueError(
                    f"{owner} request_digest must be a canonical SHA-256 digest"
                )
            object.__setattr__(
                self,
                "request_digest",
                request_digest,
            )
        object.__setattr__(
            self,
            "requested_at",
            (
                ""
                if self.requested_at == ""
                else _validate_non_empty_string(
                    owner,
                    "requested_at",
                    self.requested_at,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ServerAuthorizationDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    hide_resource: bool = False

    def __post_init__(self) -> None:
        owner = "server authorization decision"
        if not isinstance(self.allowed, bool):
            raise ValueError(f"{owner} allowed must be a boolean")
        if not isinstance(self.reason_codes, (list, tuple)):
            raise ValueError(f"{owner} reason_codes must be a sequence")
        reason_codes = tuple(
            _validate_exact_non_empty_string(
                owner,
                "reason_codes",
                reason_code,
            )
            for reason_code in self.reason_codes
        )
        if len(set(reason_codes)) != len(reason_codes):
            raise ValueError(
                f"{owner} reason_codes must not contain duplicates"
            )
        object.__setattr__(self, "reason_codes", reason_codes)
        if not isinstance(self.hide_resource, bool):
            raise ValueError(f"{owner} hide_resource must be a boolean")
        if self.allowed and self.hide_resource:
            raise ValueError(
                f"{owner} allowed decisions must not hide resources"
            )


class ServerAuthHook(Protocol):
    def authorize(self, request: ServerAuthRequest) -> ServerAuthDecision:
        ...


class ServerAuthorizationHook(Protocol):
    def authorize(
        self,
        request: ServerAuthorizationRequest,
    ) -> ServerAuthorizationDecision:
        ...


class ServerAsyncCallbackResumeAdmissionHook(Protocol):
    def admit(
        self,
        submission: ServerAsyncCallbackSubmission,
        checkpoint: RuntimeCheckpoint,
    ) -> Mapping[str, object]:
        ...


@dataclass(frozen=True, slots=True)
class _ServerRunAuthorizationRecord:
    external_run_id: str
    tenant_id: str | None
    owner_principal_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "external_run_id",
            _validate_exact_non_empty_string(
                "server run authorization record",
                "external_run_id",
                self.external_run_id,
            ),
        )
        if self.tenant_id is not None:
            object.__setattr__(
                self,
                "tenant_id",
                _validate_exact_non_empty_string(
                    "server run authorization record",
                    "tenant_id",
                    self.tenant_id,
                ),
            )
        if self.owner_principal_id is not None:
            object.__setattr__(
                self,
                "owner_principal_id",
                _validate_exact_non_empty_string(
                    "server run authorization record",
                    "owner_principal_id",
                    self.owner_principal_id,
                ),
            )
        object.__setattr__(
            self,
            "created_at",
            _validate_iso_datetime(
                "server run authorization record",
                "created_at",
                self.created_at,
            ),
        )

    @classmethod
    def create(
        cls,
        external_run_id: str,
        principal: PrincipalRef | None,
        created_at: str,
    ) -> _ServerRunAuthorizationRecord:
        return cls(
            external_run_id=external_run_id,
            tenant_id=principal.tenant_id if principal is not None else None,
            owner_principal_id=(
                principal.principal_id if principal is not None else None
            ),
            created_at=created_at,
        )

    def allows_read(self, principal: PrincipalRef | None) -> bool:
        if self.owner_principal_id is None:
            return principal is None
        if (
            principal is not None
            and principal.principal_id == self.owner_principal_id
            and principal.tenant_id == self.tenant_id
        ):
            return True
        return (
            principal is not None
            and principal.tenant_id == self.tenant_id
            and "operator" in principal.roles
        )


@dataclass(frozen=True, slots=True)
class _AcceptedRunAdmissionReservation:
    authorization: _ServerRunAuthorizationRecord
    reservation_id: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.authorization,
            _ServerRunAuthorizationRecord,
        ):
            raise ValueError(
                "server accepted run reservation authorization must be "
                "a run authorization record"
            )
        object.__setattr__(
            self,
            "reservation_id",
            _validate_exact_non_empty_string(
                "server accepted run reservation",
                "reservation_id",
                self.reservation_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class _ServerRetiredRunRecord:
    authorization: _ServerRunAuthorizationRecord
    previous_state: str
    last_cursor: str
    deleted_at: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.authorization,
            _ServerRunAuthorizationRecord,
        ):
            raise ValueError(
                "server retired run authorization must be a run authorization record"
            )
        if self.previous_state not in SERVER_TERMINAL_RUN_STATES:
            raise ValueError(
                "server retired run previous_state must be terminal"
            )
        object.__setattr__(
            self,
            "last_cursor",
            _validate_run_cursor(
                "server retired run",
                "last_cursor",
                self.authorization.external_run_id,
                self.last_cursor,
            ),
        )
        object.__setattr__(
            self,
            "deleted_at",
            _validate_iso_datetime(
                "server retired run",
                "deleted_at",
                self.deleted_at,
            ),
        )

    def response_payload(self, *, duplicate: bool) -> dict[str, object]:
        return {
            "ok": True,
            "runId": self.authorization.external_run_id,
            "status": "deleted",
            "previousState": self.previous_state,
            "lastCursor": self.last_cursor,
            "deletedAt": self.deleted_at,
            "duplicate": duplicate,
        }


class _ServerRunDeletionConflictError(RuntimeError):
    def __init__(self, run_id: str, state: str) -> None:
        self.run_id = run_id
        self.state = state
        super().__init__(
            f"run {run_id!r} in state {state!r} cannot be deleted"
        )


@dataclass(frozen=True, slots=True)
class StaticBearerAuthHook:
    principals_by_token: Mapping[str, PrincipalRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.principals_by_token, Mapping):
            raise ValueError("static bearer auth principals_by_token must be a mapping")
        principals_by_token: dict[str, PrincipalRef] = {}
        for token, principal in self.principals_by_token.items():
            token = _validate_non_empty_string("static bearer auth", "token", token)
            if not isinstance(principal, PrincipalRef):
                raise ValueError("static bearer auth principals must be PrincipalRef instances")
            principals_by_token[token] = principal
        object.__setattr__(self, "principals_by_token", MappingProxyType(principals_by_token))

    def authorize(self, request: ServerAuthRequest) -> ServerAuthDecision:
        if not request.route.auth_required:
            return ServerAuthDecision(True)
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not separator:
            return ServerAuthDecision(False, reason_codes=("auth.missing_bearer_token",))
        token = token.strip()
        # Compare against every configured token without early exit so the
        # response time does not reveal token prefixes or membership.
        presented = token.encode("utf-8")
        principal: PrincipalRef | None = None
        for candidate_token, candidate_principal in self.principals_by_token.items():
            if compare_digest(candidate_token.encode("utf-8"), presented):
                principal = candidate_principal
        if principal is None:
            return ServerAuthDecision(False, reason_codes=("auth.invalid_bearer_token",))
        return ServerAuthDecision(True, principal=principal)


@dataclass(frozen=True, slots=True)
class ServerHealth:
    service: str
    checks: tuple[
        tuple[str, ServerHealthStatus, Mapping[str, object]],
        ...,
    ] = field(default_factory=tuple)
    observed_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", _validate_non_empty_string("server health", "service", self.service))
        if self.observed_at != "":
            object.__setattr__(
                self,
                "observed_at",
                _validate_non_empty_string("server health", "observed_at", self.observed_at),
            )
        try:
            checks = tuple(self.checks)
        except TypeError as error:
            raise ValueError("server health checks must be a collection of check records") from error
        normalized_checks: list[
            tuple[str, ServerHealthStatus, Mapping[str, object]]
        ] = []
        for check in checks:
            try:
                name, status, details = check
            except (TypeError, ValueError) as error:
                raise ValueError("server health check records must contain name, status, and details") from error
            name = _validate_non_empty_string("server health check", "name", name)
            if status not in VALID_SERVER_HEALTH_STATUSES:
                raise ValueError(f"invalid server health status {status}")
            if not isinstance(details, Mapping):
                raise ValueError("server health check details must be a mapping")
            details_copy = dict(details)
            if any(not isinstance(key, str) or not key.strip() for key in details_copy):
                raise ValueError("server health check detail keys must be non-empty strings")
            normalized_checks.append(
                (name, status, MappingProxyType(details_copy))
            )
        object.__setattr__(self, "checks", tuple(normalized_checks))

    def overall_status(self) -> ServerHealthStatus:
        statuses = {status for _, status, _ in self.checks}
        if "unhealthy" in statuses:
            return "unhealthy"
        if "degraded" in statuses:
            return "degraded"
        return "healthy"

    def to_payload(self) -> dict[str, object]:
        return {
            "service": self.service,
            "status": self.overall_status(),
            "observed_at": self.observed_at,
            "checks": {
                name: {
                    "status": status,
                    "details": dict(details),
                }
                for name, status, details in self.checks
            },
        }


@dataclass(slots=True)
class _AcceptedRunExecution:
    admission_reservation_id: str
    runtime: InProcessRuntime
    cancellation_token: CancellationToken
    journal: ExecutionJournal
    checkpoint: RuntimeCheckpoint | None = None
    callback_receipt: Mapping[str, object] | None = None
    resume_dispatch_pending: bool = False
    resume_future: Future[object] | None = None


@dataclass(frozen=True, slots=True)
class _AcceptedCallbackReceiptCapability:
    receipt_digest: str
    checkpoint_id: str
    checkpoint_state_digest: str
    release_digest: str

    def __call__(
        self,
        receipt: Mapping[str, object],
        *,
        checkpoint: RuntimeCheckpoint,
        expected_checkpoint_digest: str,
        expected_release_digest: str,
    ) -> bool:
        return (
            checkpoint.checkpoint_id == self.checkpoint_id
            and expected_checkpoint_digest == self.checkpoint_state_digest
            and expected_release_digest == self.release_digest
            and canonical_hash(_thaw_json_value(receipt)) == self.receipt_digest
        )
