from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from types import MappingProxyType
from typing import Literal, Protocol

from .application_event import ApplicationEvent, ApplicationEventMetadata
from .canonical import canonical_hash
from .policy import PrincipalRef
from .runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry


ServerTransport = Literal["http", "sse", "websocket"]
ServerHealthStatus = Literal["healthy", "degraded", "unhealthy"]
VALID_SERVER_TRANSPORTS = frozenset({"http", "sse", "websocket"})
VALID_SERVER_HEALTH_STATUSES = frozenset({"healthy", "degraded", "unhealthy"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{owner} {field_name} must not be empty")
    return stripped


def _validate_route_path(owner: str, value: object) -> str:
    path = _validate_non_empty_string(owner, "path", value)
    if not path.startswith("/"):
        raise ValueError(f"{owner} path must start with '/'")
    return path


def _validate_transport(value: object) -> ServerTransport:
    transport = _validate_non_empty_string("server", "transport", value)
    if transport not in VALID_SERVER_TRANSPORTS:
        raise ValueError("server transport must be one of http, sse, or websocket")
    return transport  # type: ignore[return-value]


def _validate_string_mapping(
    owner: str,
    field_name: str,
    value: object,
    *,
    lowercase_keys: bool = False,
) -> MappingProxyType[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        key_text = _validate_non_empty_string(owner, f"{field_name} key", key)
        if not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} values must be strings")
        normalized[key_text.lower() if lowercase_keys else key_text] = item
    return MappingProxyType(normalized)


def _validate_string_sequence(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a sequence")
    try:
        return tuple(
            sorted(
                {
                    _validate_non_empty_string(
                        owner,
                        field_name,
                        item,
                    )
                    for item in value  # type: ignore[union-attr]
                }
            )
        )
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a sequence") from error


@dataclass(frozen=True, slots=True)
class ServerEndpoint:
    method: str
    path: str
    transport: ServerTransport
    operation: str
    auth_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method",
            _validate_non_empty_string("server endpoint", "method", self.method).upper(),
        )
        object.__setattr__(self, "path", _validate_route_path("server endpoint", self.path))
        object.__setattr__(self, "transport", _validate_transport(self.transport))
        object.__setattr__(
            self,
            "operation",
            _validate_non_empty_string("server endpoint", "operation", self.operation),
        )
        if not isinstance(self.auth_required, bool):
            raise ValueError("server endpoint auth_required must be a boolean")

    def canonical_value(self) -> dict[str, object]:
        return {
            "method": self.method,
            "path": self.path,
            "transport": self.transport,
            "operation": self.operation,
            "auth_required": self.auth_required,
        }


class ServerRouteNotFoundError(KeyError):
    def __init__(self, method: str, path: str) -> None:
        self.method = method.upper()
        self.path = path
        super().__init__(f"server route {self.method} {path!r} is not defined")


@dataclass(frozen=True, slots=True)
class ServerRouteMatch:
    endpoint: ServerEndpoint
    path_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.path_params, Mapping):
            raise ValueError("server route path_params must be a mapping")
        path_params = dict(self.path_params)
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, str)
            for name, value in path_params.items()
        ):
            raise ValueError("server route path_params keys and values must be strings")
        object.__setattr__(self, "path_params", MappingProxyType(path_params))


@dataclass(frozen=True, slots=True)
class ServerRouteManifest:
    endpoints: tuple[ServerEndpoint, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        endpoints = tuple(self.endpoints)
        seen: set[tuple[str, str, ServerTransport]] = set()
        for endpoint in endpoints:
            if not isinstance(endpoint, ServerEndpoint):
                raise ValueError("server route manifest endpoints must be ServerEndpoint instances")
            key = (endpoint.method, endpoint.path, endpoint.transport)
            if key in seen:
                raise ValueError(f"duplicate server endpoint {endpoint.method} {endpoint.path} {endpoint.transport}")
            seen.add(key)
        object.__setattr__(self, "endpoints", endpoints)

    def with_endpoint(
        self,
        method: str,
        path: str,
        transport: ServerTransport,
        operation: str,
        *,
        auth_required: bool = True,
    ) -> ServerRouteManifest:
        return replace(
            self,
            endpoints=(*self.endpoints, ServerEndpoint(method, path, transport, operation, auth_required)),
        )

    def by_transport(self, transport: ServerTransport) -> tuple[ServerEndpoint, ...]:
        transport = _validate_transport(transport)
        return tuple(endpoint for endpoint in self.endpoints if endpoint.transport == transport)

    def match(self, method: str, path: str) -> ServerRouteMatch:
        normalized_method = _validate_non_empty_string("server route lookup", "method", method).upper()
        path = _validate_route_path("server route lookup", path)
        path_parts = [part for part in path.strip("/").split("/") if part]
        for endpoint in self.endpoints:
            if endpoint.method != normalized_method:
                continue
            endpoint_parts = [part for part in endpoint.path.strip("/").split("/") if part]
            if endpoint.path == path:
                return ServerRouteMatch(endpoint)
            if len(endpoint_parts) != len(path_parts):
                continue
            path_params: dict[str, str] = {}
            for template_part, path_part in zip(endpoint_parts, path_parts, strict=True):
                if template_part.startswith("{") and template_part.endswith("}"):
                    path_params[template_part[1:-1]] = path_part
                    continue
                if template_part != path_part:
                    break
            else:
                return ServerRouteMatch(endpoint, path_params)
        raise ServerRouteNotFoundError(method, path)

    def lookup(self, method: str, path: str) -> ServerEndpoint:
        return self.match(method, path).endpoint

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "endpoints": sorted(
                    (endpoint.canonical_value() for endpoint in self.endpoints),
                    key=lambda endpoint: (str(endpoint["method"]), str(endpoint["path"]), str(endpoint["transport"])),
                )
            }
        )


def default_server_route_manifest() -> ServerRouteManifest:
    return ServerRouteManifest(
        (
            ServerEndpoint("GET", "/health", "http", "health", auth_required=False),
            ServerEndpoint("GET", "/runs", "http", "list_runs", auth_required=True),
            ServerEndpoint("POST", "/runs", "http", "invoke_graph", auth_required=True),
            ServerEndpoint("GET", "/runs/{run_id}", "http", "get_run_status", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/attach", "http", "attach_to_run", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/detach", "http", "detach_from_run", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/cancel", "http", "cancel_run", auth_required=True),
            ServerEndpoint("POST", "/callbacks/{operation_id}", "http", "submit_async_callback", auth_required=True),
            ServerEndpoint("GET", "/runs/{run_id}/events", "sse", "application_events", auth_required=True),
            ServerEndpoint("GET", "/runs/{run_id}/stream", "websocket", "application_stream", auth_required=True),
        )
    )


@dataclass(frozen=True, slots=True)
class ServerAuthRequest:
    route: ServerEndpoint
    headers: dict[str, str]
    query: dict[str, str]
    cookies: dict[str, str]
    requested_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, ServerEndpoint):
            raise ValueError("server auth request route must be a ServerEndpoint")
        object.__setattr__(
            self,
            "headers",
            _validate_string_mapping("server auth request", "headers", self.headers, lowercase_keys=True),
        )
        object.__setattr__(self, "query", _validate_string_mapping("server auth request", "query", self.query))
        object.__setattr__(self, "cookies", _validate_string_mapping("server auth request", "cookies", self.cookies))
        object.__setattr__(
            self,
            "requested_at",
            ""
            if self.requested_at == ""
            else _validate_non_empty_string("server auth request", "requested_at", self.requested_at),
        )


@dataclass(frozen=True, slots=True)
class ServerRequest:
    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    cookies: dict[str, str]
    body: bytes = b""
    requested_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method",
            _validate_non_empty_string("server request", "method", self.method).upper(),
        )
        object.__setattr__(self, "path", _validate_route_path("server request", self.path))
        object.__setattr__(
            self,
            "headers",
            _validate_string_mapping("server request", "headers", self.headers, lowercase_keys=True),
        )
        object.__setattr__(self, "query", _validate_string_mapping("server request", "query", self.query))
        object.__setattr__(self, "cookies", _validate_string_mapping("server request", "cookies", self.cookies))
        if not isinstance(self.body, (bytes, bytearray, memoryview)):
            raise ValueError("server request body must be bytes")
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(
            self,
            "requested_at",
            ""
            if self.requested_at == ""
            else _validate_non_empty_string("server request", "requested_at", self.requested_at),
        )


@dataclass(frozen=True, slots=True)
class ServerResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise ValueError("server response status_code must be an integer")
        if self.status_code < 100 or self.status_code > 599:
            raise ValueError("server response status_code must be a valid HTTP status")
        object.__setattr__(
            self,
            "headers",
            _validate_string_mapping("server response", "headers", self.headers, lowercase_keys=True),
        )
        if not isinstance(self.body, (bytes, bytearray, memoryview)):
            raise ValueError("server response body must be bytes")
        object.__setattr__(self, "body", bytes(self.body))

    def read(self) -> bytes:
        return self.body

    @classmethod
    def json(cls, status_code: int, payload: Mapping[str, object]) -> ServerResponse:
        if not isinstance(payload, Mapping):
            raise ValueError("server response JSON payload must be a mapping")
        payload_copy = dict(payload)
        if any(not isinstance(key, str) or not key.strip() for key in payload_copy):
            raise ValueError("server response JSON payload keys must be non-empty strings")
        return cls(
            status_code=status_code,
            headers={"content-type": "application/json"},
            body=json.dumps(payload_copy, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )


@dataclass(frozen=True, slots=True)
class ServerAsyncCallbackSubmission:
    operation_id: str
    callback_id: str
    idempotency_key: str
    payload: Mapping[str, object]
    run_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    provider_operation_id: str | None = None
    received_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _validate_non_empty_string("server async callback", "operation_id", self.operation_id),
        )
        object.__setattr__(
            self,
            "callback_id",
            _validate_non_empty_string("server async callback", "callback_id", self.callback_id),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_non_empty_string("server async callback", "idempotency_key", self.idempotency_key),
        )
        if not isinstance(self.payload, Mapping):
            raise ValueError("server async callback payload must be a JSON object")
        payload = dict(self.payload)
        if any(not isinstance(key, str) or not key.strip() for key in payload):
            raise ValueError("server async callback payload keys must be non-empty strings")
        object.__setattr__(self, "payload", MappingProxyType(payload))
        for field_name in ("run_id", "node_id", "attempt_id", "provider_operation_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _validate_non_empty_string("server async callback", field_name, value),
                )
        if self.received_at != "":
            object.__setattr__(
                self,
                "received_at",
                _validate_non_empty_string("server async callback", "received_at", self.received_at),
            )

    @classmethod
    def from_request(
        cls,
        *,
        operation_id: str,
        request: ServerRequest,
    ) -> ServerAsyncCallbackSubmission:
        body = json.loads(request.body.decode("utf-8") or "{}")
        if not isinstance(body, Mapping):
            raise ValueError("server async callback body must be a JSON object")
        headers = request.headers
        idempotency_key = body.get(
            "idempotency_key",
            body.get(
                "idempotencyKey",
                headers.get("graphblocks-idempotency-key", headers.get("idempotency-key", "")),
            ),
        )
        payload = body.get("payload")
        if payload is None:
            raise ValueError("server async callback payload is required")
        return cls(
            operation_id=operation_id,
            callback_id=_validate_non_empty_string(
                "server async callback",
                "callback_id",
                body.get("callback_id", body.get("callbackId", "")),
            ),
            idempotency_key=_validate_non_empty_string(
                "server async callback",
                "idempotency_key",
                idempotency_key,
            ),
            payload=payload,
            run_id=_optional_callback_string(body, "run_id", "runId"),
            node_id=_optional_callback_string(body, "node_id", "nodeId"),
            attempt_id=_optional_callback_string(body, "attempt_id", "attemptId"),
            provider_operation_id=_optional_callback_string(
                body,
                "provider_operation_id",
                "providerOperationId",
            ),
            received_at=request.requested_at or _utc_now_iso(),
        )

    def response_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "operationId": self.operation_id,
            "callbackId": self.callback_id,
            "idempotencyKey": self.idempotency_key,
            "status": "accepted",
        }

    def duplicate_response_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "operationId": self.operation_id,
            "callbackId": self.callback_id,
            "idempotencyKey": self.idempotency_key,
            "status": "duplicate",
            "duplicate": True,
        }


def _optional_callback_string(body: Mapping[str, object], snake: str, camel: str) -> str | None:
    value = body.get(snake, body.get(camel))
    if value is None:
        return None
    return _validate_non_empty_string("server async callback", snake, value)


@dataclass(frozen=True, slots=True)
class ServerAuthDecision:
    allowed: bool
    principal: PrincipalRef | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


class ServerAuthHook(Protocol):
    def authorize(self, request: ServerAuthRequest) -> ServerAuthDecision:
        ...


@dataclass(frozen=True, slots=True)
class StaticBearerAuthHook:
    principals_by_token: dict[str, PrincipalRef] = field(default_factory=dict)

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
        if not authorization.startswith("Bearer "):
            return ServerAuthDecision(False, reason_codes=("auth.missing_bearer_token",))
        token = authorization.removeprefix("Bearer ").strip()
        principal = self.principals_by_token.get(token)
        if principal is None:
            return ServerAuthDecision(False, reason_codes=("auth.invalid_bearer_token",))
        return ServerAuthDecision(True, principal=principal)


@dataclass(frozen=True, slots=True)
class ServerHealth:
    service: str
    checks: tuple[tuple[str, ServerHealthStatus, dict[str, object]], ...] = field(default_factory=tuple)
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
        normalized_checks: list[tuple[str, ServerHealthStatus, MappingProxyType[str, object]]] = []
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
            normalized_checks.append((name, status, MappingProxyType(details_copy)))  # type: ignore[arg-type]
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
class GraphBlocksServerApp:
    route_manifest: ServerRouteManifest = field(default_factory=default_server_route_manifest)
    auth_hook: ServerAuthHook | None = None
    health: ServerHealth = field(default_factory=lambda: ServerHealth("graphblocks-api"))
    registry: RuntimeRegistry = field(default_factory=stdlib_registry)
    _events_by_run_id: dict[str, tuple[dict[str, object], ...]] = field(default_factory=dict, init=False, repr=False)
    _callbacks_by_operation_id: dict[str, tuple[ServerAsyncCallbackSubmission, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _detachments_by_run_id: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def handle(self, request: ServerRequest) -> ServerResponse:
        try:
            route_match = self.route_manifest.match(request.method, request.path)
            route = route_match.endpoint
        except ServerRouteNotFoundError as error:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": str(error),
                },
            )

        if self.auth_hook is not None:
            auth_decision = self.auth_hook.authorize(
                ServerAuthRequest(
                    route=route,
                    headers=request.headers,
                    query=request.query,
                    cookies=request.cookies,
                    requested_at=request.requested_at,
                )
            )
            if not auth_decision.allowed:
                return ServerResponse.json(
                    401,
                    {
                        "ok": False,
                        "reasonCodes": list(auth_decision.reason_codes),
                    },
                )

        if route.operation == "health":
            return ServerResponse.json(200, self.health.to_payload())
        if route.operation == "list_runs":
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runs": [
                        self._run_status_payload(run_id, events, include_ok=False)
                        for run_id, events in sorted(self._events_by_run_id.items())
                    ],
                },
            )
        if route.operation == "cancel_run":
            return ServerResponse.json(
                202,
                {
                    "ok": True,
                    "runId": route_match.path_params.get("run_id", ""),
                    "status": "cancel_requested",
                },
            )
        if route.operation == "get_run_status":
            run_id = route_match.path_params.get("run_id", "")
            events = self._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run status not found for run {run_id!r}",
                    },
                )
            return ServerResponse.json(200, self._run_status_payload(run_id, events))
        if route.operation == "attach_to_run":
            try:
                run_id = route_match.path_params.get("run_id", "")
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run attach stream not found for run {run_id!r}",
                        },
                    )
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, Mapping):
                    raise ValueError("attach request body must be a JSON object")
                return self._attach_to_run_response(run_id, events, payload)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "detach_from_run":
            try:
                run_id = route_match.path_params.get("run_id", "")
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run detach stream not found for run {run_id!r}",
                        },
                    )
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, Mapping):
                    raise ValueError("detach request body must be a JSON object")
                return self._detach_from_run_response(run_id, events, payload, request.requested_at or _utc_now_iso())
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "submit_async_callback":
            try:
                submission = ServerAsyncCallbackSubmission.from_request(
                    operation_id=route_match.path_params.get("operation_id", ""),
                    request=request,
                )
                existing = self._callbacks_by_operation_id.get(submission.operation_id, ())
                for previous in existing:
                    if previous.idempotency_key == submission.idempotency_key:
                        if (
                            previous.callback_id != submission.callback_id
                            or dict(previous.payload) != dict(submission.payload)
                            or previous.run_id != submission.run_id
                            or previous.node_id != submission.node_id
                            or previous.attempt_id != submission.attempt_id
                            or previous.provider_operation_id != submission.provider_operation_id
                        ):
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
                self._callbacks_by_operation_id[submission.operation_id] = (*existing, submission)
                return ServerResponse.json(202, submission.response_payload())
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "application_events":
            run_id = route_match.path_params.get("run_id", "")
            events = self._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"run events not found for run {run_id!r}",
                    },
                )
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "events": [dict(event) for event in events],
                },
            )
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
            events = self._events_by_run_id.get(run_id)
            if events is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"application stream not found for run {run_id!r}",
                    },
                )
            last_sequence = 0
            for event in events:
                metadata = event.get("metadata")
                if isinstance(metadata, dict):
                    sequence = metadata.get("sequence", 0)
                    if isinstance(sequence, int) and sequence > last_sequence:
                        last_sequence = sequence
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "stream": {
                        "transport": "websocket",
                        "status": "accepted",
                        "cursor": f"{run_id}:{last_sequence}",
                        "eventCount": len(events),
                    },
                    "events": [dict(event) for event in events],
                },
            )
        if route.operation == "invoke_graph":
            try:
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("run request body must be a JSON object")
                graph = payload.get("graph")
                if not isinstance(graph, dict):
                    raise ValueError("run request body requires graph object")
                inputs = payload.get("inputs", {})
                if not isinstance(inputs, dict):
                    raise ValueError("run request inputs must be a JSON object")
                run_id = _validate_non_empty_string(
                    "run request",
                    "runId",
                    payload.get("runId", payload.get("run_id", "run-000001")),
                )
                response_id = _validate_non_empty_string(
                    "run request",
                    "responseId",
                    payload.get("responseId", payload.get("response_id", "response-000001")),
                )
                release_id = _validate_non_empty_string(
                    "run request",
                    "releaseId",
                    payload.get("releaseId", payload.get("release_id", "local")),
                )
                policy_snapshot_id = _validate_non_empty_string(
                    "run request",
                    "policySnapshotId",
                    payload.get("policySnapshotId", payload.get("policy_snapshot_id", "local")),
                )
                occurred_at = payload.get("occurredAt", payload.get("occurred_at"))
                if occurred_at is None:
                    occurred_at = _utc_now_iso()
                if not isinstance(occurred_at, str):
                    raise ValueError("run request occurredAt must be a string")
                if not occurred_at.strip():
                    raise ValueError("run request occurredAt must not be empty")
                turn_id_value = payload.get("turnId", payload.get("turn_id"))
                turn_id = (
                    _validate_non_empty_string("run request", "turnId", turn_id_value)
                    if turn_id_value is not None
                    else None
                )

                result = InProcessRuntime(self.registry).run(graph, inputs, run_id=run_id)
                start_payload = result.journal.records[0].payload if result.journal.records else {}
                start_event = ApplicationEvent.new(
                    "RunStarted",
                    ApplicationEventMetadata(
                        event_id=f"{result.run_id}:run-started",
                        run_id=result.run_id,
                        response_id=response_id,
                        turn_id=turn_id,
                        sequence=1,
                        release_id=release_id,
                        policy_snapshot_id=policy_snapshot_id,
                        occurred_at=occurred_at,
                    ),
                    payload={
                        "status": "running",
                        "graph_hash": str(start_payload.get("graphHash", "")),
                    },
                )
                terminal_kind = {
                    "succeeded": "RunSucceeded",
                    "failed": "RunFailed",
                    "cancelled": "RunCancelled",
                }[result.status]
                terminal_payload: dict[str, object] = {"status": result.status, "outputs": dict(result.outputs)}
                if result.status == "cancelled":
                    terminal_payload = {"status": result.status, "reason": "cancelled"}
                elif result.status == "failed" and result.journal.records:
                    terminal_payload.update(dict(result.journal.records[-1].payload))
                terminal_event = ApplicationEvent.new(
                    terminal_kind,
                    ApplicationEventMetadata(
                        event_id=f"{result.run_id}:run-terminal",
                        run_id=result.run_id,
                        response_id=response_id,
                        turn_id=turn_id,
                        sequence=2,
                        release_id=release_id,
                        policy_snapshot_id=policy_snapshot_id,
                        occurred_at=occurred_at,
                    ),
                    payload=terminal_payload,
                )
                events = []
                for event in (start_event, terminal_event):
                    event_payload: dict[str, object] = {
                        "kind": event.kind,
                        "metadata": {
                            "eventId": event.metadata.event_id,
                            "runId": event.metadata.run_id,
                            "responseId": event.metadata.response_id,
                            "turnId": event.metadata.turn_id,
                            "sequence": event.metadata.sequence,
                            "releaseId": event.metadata.release_id,
                            "policySnapshotId": event.metadata.policy_snapshot_id,
                            "occurredAt": event.metadata.occurred_at,
                        },
                        "payload": dict(event.payload),
                    }
                    if event.tool_call_id is not None:
                        event_payload["toolCallId"] = event.tool_call_id
                    events.append(event_payload)
                self._events_by_run_id[result.run_id] = tuple(dict(event) for event in events)
                return ServerResponse.json(
                    200,
                    {
                        "runId": result.run_id,
                        "status": result.status,
                        "outputs": dict(result.outputs),
                        "events": events,
                    },
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        return ServerResponse.json(
            501,
            {
                "ok": False,
                "error": f"server operation {route.operation!r} is not implemented",
            },
        )

    def callback_submissions(self, operation_id: str) -> tuple[ServerAsyncCallbackSubmission, ...]:
        operation_id = _validate_non_empty_string("server async callback", "operation_id", operation_id)
        return self._callbacks_by_operation_id.get(operation_id, ())

    def detachments(self, run_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_non_empty_string("server detach", "run_id", run_id)
        return self._detachments_by_run_id.get(run_id, ())

    def _run_status_payload(
        self,
        run_id: str,
        events: tuple[dict[str, object], ...],
        *,
        include_ok: bool = True,
    ) -> dict[str, object]:
        last_sequence = 0
        release_id = ""
        started_at = ""
        updated_at = ""
        completed_at: str | None = None
        state = "running"
        terminal_states = {
            "RunSucceeded": "succeeded",
            "RunFailed": "failed",
            "RunCancelled": "cancelled",
            "RunPolicyStopped": "policy_stopped",
        }

        for index, event in enumerate(events):
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > last_sequence:
                last_sequence = sequence
            occurred_at = metadata.get("occurredAt")
            if isinstance(occurred_at, str) and occurred_at:
                if index == 0:
                    started_at = occurred_at
                updated_at = occurred_at
            event_release_id = metadata.get("releaseId")
            if isinstance(event_release_id, str) and event_release_id:
                release_id = event_release_id
            event_kind = event.get("kind")
            if isinstance(event_kind, str) and event_kind in terminal_states:
                state = terminal_states[event_kind]
                completed_at = updated_at

        payload: dict[str, object] = {
            "runId": run_id,
            "state": state,
            "releaseId": release_id,
            "lastCursor": f"{run_id}:{last_sequence}",
            "startedAt": started_at,
            "updatedAt": updated_at,
            "completedAt": completed_at,
            "waitingOn": [],
            "activeOperations": [],
        }
        if include_ok:
            return {"ok": True, **payload}
        return payload

    def _attach_to_run_response(
        self,
        run_id: str,
        events: tuple[dict[str, object], ...],
        payload: Mapping[str, object],
    ) -> ServerResponse:
        last_cursor = payload.get("last_cursor", payload.get("lastCursor"))
        if last_cursor is not None:
            last_cursor = _validate_non_empty_string("attach request", "last_cursor", last_cursor)
        capabilities = payload.get("capabilities", ())
        if capabilities is None:
            capabilities = ()
        capabilities_tuple = _validate_string_sequence("attach request", "capabilities", capabilities)

        sequence_by_cursor: dict[str, int] = {}
        last_sequence = 0
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                continue
            cursor = f"{run_id}:{sequence}"
            sequence_by_cursor[cursor] = sequence
            if sequence > last_sequence:
                last_sequence = sequence

        replay_after_sequence = 0
        if last_cursor is not None:
            if last_cursor not in sequence_by_cursor:
                nearest_cursor = f"{run_id}:{min(sequence_by_cursor.values())}" if sequence_by_cursor else None
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "error": "CursorExpired",
                        "runId": run_id,
                        "requestedCursor": last_cursor,
                        "nearestAvailableCursor": nearest_cursor,
                        "lastCursor": f"{run_id}:{last_sequence}",
                        "lastSequence": last_sequence,
                    },
                )
            replay_after_sequence = sequence_by_cursor[last_cursor]

        replayed_events = []
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > replay_after_sequence:
                replayed_events.append(dict(event))

        last_cursor_value = f"{run_id}:{last_sequence}"
        return ServerResponse.json(
            200,
            {
                "ok": True,
                "runId": run_id,
                "lastCursor": last_cursor_value,
                "liveCursor": last_cursor_value,
                "replayComplete": True,
                "capabilities": list(capabilities_tuple),
                "events": replayed_events,
            },
        )

    def _detach_from_run_response(
        self,
        run_id: str,
        events: tuple[dict[str, object], ...],
        payload: Mapping[str, object],
        detached_at: str,
    ) -> ServerResponse:
        client_id = _validate_non_empty_string(
            "detach request",
            "client_id",
            payload.get("client_id", payload.get("clientId", "")),
        )
        reason_value = payload.get("reason")
        reason = (
            _validate_non_empty_string("detach request", "reason", reason_value)
            if reason_value is not None
            else None
        )
        last_sequence = self._last_event_sequence(events)
        last_cursor = f"{run_id}:{last_sequence}"
        record: dict[str, object] = {
            "clientId": client_id,
            "reason": reason,
            "detachedAt": detached_at,
            "lastCursor": last_cursor,
        }
        existing = self._detachments_by_run_id.get(run_id, ())
        self._detachments_by_run_id[run_id] = (*existing, record)
        return ServerResponse.json(
            202,
            {
                "ok": True,
                "runId": run_id,
                "clientId": client_id,
                "reason": reason,
                "status": "detached",
                "lastCursor": last_cursor,
            },
        )

    def _last_event_sequence(self, events: tuple[dict[str, object], ...]) -> int:
        last_sequence = 0
        for event in events:
            metadata = event.get("metadata")
            if isinstance(metadata, Mapping):
                sequence = metadata.get("sequence")
                if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > last_sequence:
                    last_sequence = sequence
        return last_sequence


class ServerProtocolVersionMismatchError(ValueError):
    def __init__(self, left: str, right: str) -> None:
        self.left = left
        self.right = right
        super().__init__(f"application protocol version mismatch: {left!r} != {right!r}")


@dataclass(frozen=True, slots=True)
class ApplicationProtocolCapabilities:
    protocol_version: str
    commands: tuple[str, ...] = field(default_factory=tuple)
    events: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "protocol_version",
            _validate_non_empty_string(
                "application protocol capabilities",
                "protocol_version",
                self.protocol_version,
            ),
        )
        for field_name in ("commands", "events"):
            object.__setattr__(
                self,
                field_name,
                _validate_string_sequence(
                    "application protocol capabilities",
                    field_name,
                    getattr(self, field_name),
                ),
            )

    def with_commands(self, commands: list[str] | tuple[str, ...]) -> ApplicationProtocolCapabilities:
        return replace(
            self,
            commands=_validate_string_sequence("application protocol capabilities", "commands", commands),
        )

    def with_events(self, events: list[str] | tuple[str, ...]) -> ApplicationProtocolCapabilities:
        return replace(
            self,
            events=_validate_string_sequence("application protocol capabilities", "events", events),
        )

    def negotiate(self, peer: ApplicationProtocolCapabilities) -> ApplicationProtocolCapabilities:
        if not isinstance(peer, ApplicationProtocolCapabilities):
            raise ValueError("application protocol negotiation peer must be ApplicationProtocolCapabilities")
        if self.protocol_version != peer.protocol_version:
            raise ServerProtocolVersionMismatchError(self.protocol_version, peer.protocol_version)
        return ApplicationProtocolCapabilities(
            protocol_version=self.protocol_version,
            commands=tuple(sorted(set(self.commands).intersection(peer.commands))),
            events=tuple(sorted(set(self.events).intersection(peer.events))),
        )


__all__ = [
    "ApplicationProtocolCapabilities",
    "GraphBlocksServerApp",
    "ServerAuthDecision",
    "ServerAsyncCallbackSubmission",
    "ServerAuthHook",
    "ServerAuthRequest",
    "ServerEndpoint",
    "ServerHealth",
    "ServerHealthStatus",
    "ServerProtocolVersionMismatchError",
    "ServerRequest",
    "ServerResponse",
    "ServerRouteMatch",
    "ServerRouteManifest",
    "ServerRouteNotFoundError",
    "ServerTransport",
    "StaticBearerAuthHook",
    "default_server_route_manifest",
]
