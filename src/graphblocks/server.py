from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import ipaddress
import json
import math
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.parse import urlparse

from .application_event import ApplicationEvent, ApplicationEventMetadata
from .canonical import canonical_dumps, canonical_hash
from .policy import PrincipalRef
from .runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry


ServerTransport = Literal["http", "sse", "websocket"]
ServerHealthStatus = Literal["healthy", "degraded", "unhealthy"]
VALID_SERVER_TRANSPORTS = frozenset({"http", "sse", "websocket"})
VALID_SERVER_HEALTH_STATUSES = frozenset({"healthy", "degraded", "unhealthy"})
VALID_CALLBACK_SUBSCRIPTION_SCOPES = frozenset({
    "run",
    "conversation",
    "project",
    "tenant",
    "deployment",
})
VALID_CALLBACK_FAILURE_POLICIES = frozenset({
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
})
VALID_CALLBACK_DELIVERY_KINDS = frozenset({
    "webhook",
    "websocket",
    "sse",
    "push_notification",
    "email",
    "local_callback",
})
VALID_WEBHOOK_SIGNING_ALGORITHMS = frozenset({"hmac-sha256", "ed25519"})
FORBIDDEN_WEBHOOK_HOSTS = frozenset({"localhost", "metadata.google.internal"})
SERVER_EVENT_SEVERITY_RANKS = {
    "debug": 10,
    "info": 20,
    "notice": 30,
    "warning": 40,
    "warn": 40,
    "error": 50,
    "critical": 60,
    "fatal": 60,
}
SERVER_TERMINAL_EVENT_KINDS = frozenset({
    "RunSucceeded",
    "RunFailed",
    "RunCancelled",
    "RunPolicyStopped",
    "RunCompleted",
})


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


def _validate_callback_subscription_scope(value: object) -> str:
    scope = _validate_non_empty_string("server callback registration", "scope", value)
    if scope not in VALID_CALLBACK_SUBSCRIPTION_SCOPES:
        raise ValueError(
            "server callback registration scope must be one of run, conversation, project, tenant, or deployment"
        )
    return scope


def _validate_callback_failure_policy(value: object) -> str:
    failure_policy = _validate_non_empty_string("server subscription", "failure_policy", value)
    if failure_policy not in VALID_CALLBACK_FAILURE_POLICIES:
        raise ValueError(
            "server subscription failure_policy must be one of best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure"
        )
    return failure_policy


def _webhook_url_is_unsafe(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"file", "unix"}:
        return True
    if parsed.scheme not in {"http", "https", "secret"}:
        return True
    if parsed.scheme == "secret":
        return False
    host = parsed.hostname
    if host is None:
        return True
    normalized_host = host.rstrip(".").lower()
    if normalized_host in FORBIDDEN_WEBHOOK_HOSTS or normalized_host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _validate_callback_delivery_target(owner: str, delivery: Mapping[str, object]) -> None:
    delivery_kind = _validate_non_empty_string(owner, "delivery.kind", delivery.get("kind", ""))
    if delivery_kind not in VALID_CALLBACK_DELIVERY_KINDS:
        raise ValueError(
            f"{owner} delivery.kind must be one of webhook, websocket, sse, push_notification, email, or local_callback"
        )
    if delivery_kind != "webhook":
        return
    url = _validate_non_empty_string(owner, "delivery.url", delivery.get("url", ""))
    if _webhook_url_is_unsafe(url):
        raise ValueError(f"{owner} delivery.url is unsafe or forbidden by default egress policy")
    signing = delivery.get("signing")
    if not isinstance(signing, Mapping):
        raise ValueError(f"{owner} delivery.signing must be a mapping for webhook delivery")
    algorithm = _validate_non_empty_string(owner, "delivery.signing.algorithm", signing.get("algorithm", ""))
    if algorithm not in VALID_WEBHOOK_SIGNING_ALGORITHMS:
        raise ValueError(f"{owner} delivery.signing.algorithm must be one of hmac-sha256 or ed25519")
    _validate_non_empty_string(
        owner,
        "delivery.signing.secret_ref",
        signing.get("secret_ref", signing.get("secretRef", "")),
    )


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


def _freeze_json_value(owner: str, field_name: str, value: object) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{owner} {field_name} keys must be non-empty strings")
            frozen[key] = _freeze_json_value(owner, f"{field_name}.{key}", item)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(owner, field_name, item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {field_name} must be finite")
        return value
    raise ValueError(f"{owner} {field_name} must be a JSON value")


def _validate_server_event_filter(owner: str, event_filter: Mapping[str, object]) -> None:
    for source_key, field_name in (
        ("types", "types"),
        ("visibility", "visibility"),
        ("node_ids", "node_ids"),
        ("nodeIds", "node_ids"),
        ("operation_ids", "operation_ids"),
        ("operationIds", "operation_ids"),
    ):
        if source_key in event_filter:
            _validate_string_sequence(owner, f"event_filter.{field_name}", event_filter[source_key])

    severity_min = event_filter.get("severity_min", event_filter.get("severityMin"))
    if severity_min is not None:
        severity_min_text = _validate_non_empty_string(
            owner,
            "event_filter.severity_min",
            severity_min,
        )
        if severity_min_text not in SERVER_EVENT_SEVERITY_RANKS:
            raise ValueError(f"{owner} event_filter.severity_min is invalid")

    include_terminal_events = event_filter.get(
        "include_terminal_events",
        event_filter.get("includeTerminalEvents"),
    )
    if include_terminal_events is not None and not isinstance(include_terminal_events, bool):
        raise ValueError(f"{owner} event_filter.include_terminal_events must be a boolean")


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _response_json_object(value: object) -> dict[str, object]:
    thawed = _thaw_json_value(value)
    if not isinstance(thawed, dict):
        raise ValueError("server response value must thaw to a JSON object")
    return thawed


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
            ServerEndpoint("POST", "/runs/{run_id}/subscriptions", "http", "subscribe_events", auth_required=True),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/subscriptions/{subscription_id}/ack",
                "http",
                "ack_event",
                auth_required=True,
            ),
            ServerEndpoint(
                "DELETE",
                "/runs/{run_id}/subscriptions/{subscription_id}",
                "http",
                "unsubscribe_events",
                auth_required=True,
            ),
            ServerEndpoint("POST", "/runs/{run_id}/cancel", "http", "cancel_run", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/pause", "http", "pause_run", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/resume", "http", "resume_run", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/expire", "http", "expire_run", auth_required=True),
            ServerEndpoint("POST", "/callbacks/register", "http", "register_callback", auth_required=True),
            ServerEndpoint("DELETE", "/callbacks/{subscription_id}", "http", "revoke_callback", auth_required=True),
            ServerEndpoint(
                "POST",
                "/callbacks/deliveries/{delivery_id}/redrive",
                "http",
                "redrive_callback_delivery",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/callbacks/deliveries/{delivery_id}/dead-letter",
                "http",
                "move_callback_to_dead_letter",
                auth_required=True,
            ),
            ServerEndpoint("POST", "/callbacks/{operation_id}", "http", "submit_async_callback", auth_required=True),
            ServerEndpoint("GET", "/runs/{run_id}/events", "sse", "application_events", auth_required=True),
            ServerEndpoint("GET", "/runs/{run_id}/ws", "websocket", "application_stream", auth_required=True),
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
        object.__setattr__(
            self,
            "payload",
            _freeze_json_value("server async callback", "payload", self.payload),
        )
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _validate_non_empty_string("server event subscription", "subscription_id", self.subscription_id),
        )
        object.__setattr__(
            self,
            "run_id",
            _validate_non_empty_string("server event subscription", "run_id", self.run_id),
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
            _validate_non_empty_string("server event subscription", "status", self.status),
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
                _validate_non_empty_string(
                    "server event subscription",
                    "replay_from_cursor",
                    self.replay_from_cursor,
                ),
            )
        if self.created_at != "":
            object.__setattr__(
                self,
                "created_at",
                _validate_non_empty_string("server event subscription", "created_at", self.created_at),
            )

    @classmethod
    def from_request(
        cls,
        *,
        run_id: str,
        request: ServerRequest,
        ordinal: int,
    ) -> ServerEventSubscription:
        body = json.loads(request.body.decode("utf-8") or "{}")
        if not isinstance(body, Mapping):
            raise ValueError("subscribe request body must be a JSON object")
        event_filter = body.get("event_filter", body.get("eventFilter", {}))
        delivery = body.get("delivery", {})
        if not isinstance(event_filter, Mapping):
            raise ValueError("subscribe request event_filter must be a JSON object")
        if not isinstance(delivery, Mapping):
            raise ValueError("subscribe request delivery must be a JSON object")
        subscription_id = body.get("subscription_id", body.get("subscriptionId"))
        if subscription_id is None:
            subscription_id = f"sub-{run_id}-{ordinal:06d}"
        replay_from_cursor = body.get("replay_from_cursor", body.get("replayFromCursor"))
        failure_policy = body.get("failure_policy", body.get("failurePolicy", "retry_then_dead_letter"))
        return cls(
            subscription_id=_validate_non_empty_string(
                "server event subscription",
                "subscription_id",
                subscription_id,
            ),
            run_id=run_id,
            event_filter=event_filter,
            delivery=delivery,
            failure_policy=_validate_callback_failure_policy(failure_policy),
            replay_from_cursor=(
                _validate_non_empty_string(
                    "server event subscription",
                    "replay_from_cursor",
                    replay_from_cursor,
                )
                if replay_from_cursor is not None
                else None
            ),
            created_at=request.requested_at or _utc_now_iso(),
        )

    def response_payload(self, replayed_events: list[dict[str, object]], last_cursor: str) -> dict[str, object]:
        return {
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _validate_non_empty_string("server callback registration", "subscription_id", self.subscription_id),
        )
        object.__setattr__(
            self,
            "scope",
            _validate_non_empty_string("server callback registration", "scope", self.scope),
        )
        object.__setattr__(
            self,
            "scope_id",
            _validate_non_empty_string("server callback registration", "scope_id", self.scope_id),
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
            _validate_non_empty_string("server callback registration", "status", self.status),
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
                _validate_non_empty_string(
                    "server callback registration",
                    "replay_from_cursor",
                    self.replay_from_cursor,
                ),
            )
        if self.created_at != "":
            object.__setattr__(
                self,
                "created_at",
                _validate_non_empty_string("server callback registration", "created_at", self.created_at),
            )

    @classmethod
    def from_request(cls, *, request: ServerRequest, ordinal: int) -> ServerCallbackRegistration:
        body = json.loads(request.body.decode("utf-8") or "{}")
        if not isinstance(body, Mapping):
            raise ValueError("register callback request body must be a JSON object")
        event_filter = body.get("event_filter", body.get("eventFilter", {}))
        delivery = body.get("delivery", {})
        if not isinstance(event_filter, Mapping):
            raise ValueError("register callback request event_filter must be a JSON object")
        if not isinstance(delivery, Mapping):
            raise ValueError("register callback request delivery must be a JSON object")
        subscription_id = body.get("subscription_id", body.get("subscriptionId"))
        if subscription_id is None:
            subscription_id = f"callback-sub-{ordinal:06d}"
        replay_from_cursor = body.get("replay_from_cursor", body.get("replayFromCursor"))
        failure_policy = body.get("failure_policy", body.get("failurePolicy", "retry_then_dead_letter"))
        return cls(
            subscription_id=_validate_non_empty_string(
                "server callback registration",
                "subscription_id",
                subscription_id,
            ),
            scope=_validate_callback_subscription_scope(body.get("scope", "")),
            scope_id=_validate_non_empty_string(
                "server callback registration",
                "scope_id",
                body.get("scope_id", body.get("scopeId", "")),
            ),
            event_filter=event_filter,
            delivery=delivery,
            failure_policy=_validate_callback_failure_policy(failure_policy),
            replay_from_cursor=(
                _validate_non_empty_string(
                    "server callback registration",
                    "replay_from_cursor",
                    replay_from_cursor,
                )
                if replay_from_cursor is not None
                else None
            ),
            created_at=request.requested_at or _utc_now_iso(),
        )

    def response_payload(self, replayed_events: list[dict[str, object]], last_cursor: str | None) -> dict[str, object]:
        return {
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
    max_async_callback_payload_bytes: int = 262144
    _events_by_run_id: dict[str, tuple[Mapping[str, object], ...]] = field(default_factory=dict, init=False, repr=False)
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
    _run_controls_by_run_id: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _subscriptions_by_run_id: dict[str, tuple[ServerEventSubscription, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _acks_by_subscription: dict[tuple[str, str], tuple[dict[str, object], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _callback_registrations: dict[str, ServerCallbackRegistration] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _callback_delivery_redrives: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _callback_delivery_dead_letter_moves: dict[str, tuple[dict[str, object], ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_async_callback_payload_bytes, int)
            or isinstance(self.max_async_callback_payload_bytes, bool)
            or self.max_async_callback_payload_bytes < 1
        ):
            raise ValueError("server max_async_callback_payload_bytes must be a positive integer")

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
        if route.operation in {"cancel_run", "pause_run", "resume_run", "expire_run"}:
            try:
                run_id = route_match.path_params.get("run_id", "")
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run control stream not found for run {run_id!r}",
                        },
                    )
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, Mapping):
                    raise ValueError("run control request body must be a JSON object")
                return self._run_control_response(
                    run_id,
                    route.operation,
                    events,
                    payload,
                    request.requested_at or _utc_now_iso(),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
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
        if route.operation == "subscribe_events":
            try:
                run_id = route_match.path_params.get("run_id", "")
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run event stream not found for subscription run {run_id!r}",
                        },
                    )
                existing = self._subscriptions_by_run_id.get(run_id, ())
                subscription = ServerEventSubscription.from_request(
                    run_id=run_id,
                    request=request,
                    ordinal=len(existing) + 1,
                )
                existing_subscription = self._subscription_for(run_id, subscription.subscription_id)
                if existing_subscription is not None:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "runId": run_id,
                            "subscriptionId": subscription.subscription_id,
                            "state": existing_subscription.status,
                            "error": (
                                f"subscription {subscription.subscription_id!r} already exists for run {run_id!r}"
                            ),
                        },
                    )
                replay = self._subscription_replay(subscription, events)
                if isinstance(replay, ServerResponse):
                    return replay
                self._subscriptions_by_run_id[run_id] = (*existing, subscription)
                return ServerResponse.json(
                    201,
                    subscription.response_payload(replay, f"{run_id}:{self._last_event_sequence(events)}"),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "unsubscribe_events":
            run_id = route_match.path_params.get("run_id", "")
            subscription_id = route_match.path_params.get("subscription_id", "")
            subscriptions = self._subscriptions_by_run_id.get(run_id)
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
                    self._subscriptions_by_run_id[run_id] = (
                        *subscriptions[:index],
                        revoked,
                        *subscriptions[index + 1 :],
                    )
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
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"run event stream not found for ack run {run_id!r}",
                        },
                    )
                subscription = self._subscription_for(run_id, subscription_id)
                if subscription is None:
                    return ServerResponse.json(
                        404,
                        {
                            "ok": False,
                            "error": f"subscription {subscription_id!r} not found for run {run_id!r}",
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
                            "error": f"subscription {subscription_id!r} for run {run_id!r} is {subscription.status}",
                        },
                    )
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, Mapping):
                    raise ValueError("ack request body must be a JSON object")
                return self._ack_event_response(
                    run_id,
                    subscription_id,
                    events,
                    payload,
                    request.requested_at or _utc_now_iso(),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "register_callback":
            try:
                registration = ServerCallbackRegistration.from_request(
                    request=request,
                    ordinal=len(self._callback_registrations) + 1,
                )
                existing = self._callback_registrations.get(registration.subscription_id)
                if existing is not None:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "subscriptionId": registration.subscription_id,
                            "state": existing.status,
                            "error": f"callback registration {registration.subscription_id!r} already exists",
                        },
                    )
                replay = self._callback_registration_replay(registration)
                if isinstance(replay, ServerResponse):
                    return replay
                replayed_events, last_cursor = replay
                self._callback_registrations[registration.subscription_id] = registration
                return ServerResponse.json(201, registration.response_payload(replayed_events, last_cursor))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
        if route.operation == "revoke_callback":
            subscription_id = route_match.path_params.get("subscription_id", "")
            registration = self._callback_registrations.get(subscription_id)
            if registration is None:
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": f"callback registration {subscription_id!r} not found",
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
            self._callback_registrations[subscription_id] = revoked
            return ServerResponse.json(
                202,
                {
                    "ok": True,
                    "subscriptionId": subscription_id,
                    "status": "revoked",
                },
            )
        if route.operation in {"redrive_callback_delivery", "move_callback_to_dead_letter"}:
            try:
                delivery_id = route_match.path_params.get("delivery_id", "")
                payload = json.loads(request.body.decode("utf-8") or "{}")
                if not isinstance(payload, Mapping):
                    raise ValueError("callback delivery control request body must be a JSON object")
                return self._callback_delivery_control_response(
                    delivery_id,
                    route.operation,
                    payload,
                    request.requested_at or _utc_now_iso(),
                )
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
                payload_size_bytes = len(canonical_dumps(_thaw_json_value(submission.payload)).encode("utf-8"))
                if payload_size_bytes > self.max_async_callback_payload_bytes:
                    return ServerResponse.json(
                        413,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "payloadSizeBytes": payload_size_bytes,
                            "maxPayloadBytes": self.max_async_callback_payload_bytes,
                            "error": "async callback payload exceeds max payload bytes",
                        },
                    )
                if submission.run_id is not None and submission.run_id not in self._events_by_run_id:
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
                    return ServerResponse.json(
                        400,
                        {
                            "ok": False,
                            "operationId": submission.operation_id,
                            "runId": submission.run_id,
                            "error": "async callback attempt_id is required when run_id is declared",
                        },
                    )
                if submission.run_id is not None:
                    run_status = self._run_status_payload(
                        submission.run_id,
                        self._events_by_run_id[submission.run_id],
                        include_ok=False,
                    )
                    state = run_status.get("state")
                    if state in {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}:
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
                    if (
                        previous.run_id is not None
                        and submission.run_id is not None
                        and (previous.run_id != submission.run_id or previous.attempt_id != submission.attempt_id)
                    ):
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
                    "events": [_response_json_object(event) for event in events],
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
                if isinstance(metadata, Mapping):
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
                    "events": [_response_json_object(event) for event in events],
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
                response_mode = _validate_non_empty_string(
                    "run request",
                    "responseMode",
                    payload.get("responseMode", payload.get("response_mode", "sync")),
                )
                if response_mode not in {"sync", "accepted", "background"}:
                    raise ValueError("run request responseMode must be one of sync, accepted, or background")
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
                self._events_by_run_id[result.run_id] = tuple(
                    _freeze_json_value("application event stream", "event", event)
                    for event in events
                )
                if response_mode in {"accepted", "background"}:
                    return ServerResponse.json(
                        202,
                        {
                            "ok": True,
                            "runId": result.run_id,
                            "status": response_mode,
                            "eventStream": f"/runs/{result.run_id}/events",
                            "websocket": f"/runs/{result.run_id}/ws",
                            "cancel": f"/runs/{result.run_id}/cancel",
                            "initialCursor": f"{result.run_id}:0",
                        },
                    )
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

    def run_controls(self, run_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_non_empty_string("server run control", "run_id", run_id)
        return self._run_controls_by_run_id.get(run_id, ())

    def subscriptions(self, run_id: str) -> tuple[ServerEventSubscription, ...]:
        run_id = _validate_non_empty_string("server event subscription", "run_id", run_id)
        return self._subscriptions_by_run_id.get(run_id, ())

    def event_acks(self, run_id: str, subscription_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_non_empty_string("server event ack", "run_id", run_id)
        subscription_id = _validate_non_empty_string("server event ack", "subscription_id", subscription_id)
        return self._acks_by_subscription.get((run_id, subscription_id), ())

    def callback_registrations(self) -> tuple[ServerCallbackRegistration, ...]:
        return tuple(self._callback_registrations[key] for key in sorted(self._callback_registrations))

    def callback_delivery_redrives(self, delivery_id: str) -> tuple[dict[str, object], ...]:
        delivery_id = _validate_non_empty_string("server callback delivery control", "delivery_id", delivery_id)
        return self._callback_delivery_redrives.get(delivery_id, ())

    def callback_delivery_dead_letter_moves(self, delivery_id: str) -> tuple[dict[str, object], ...]:
        delivery_id = _validate_non_empty_string("server callback delivery control", "delivery_id", delivery_id)
        return self._callback_delivery_dead_letter_moves.get(delivery_id, ())

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

        controls = self._run_controls_by_run_id.get(run_id, ())
        if controls:
            latest_control = controls[-1]
            control_status = latest_control.get("status")
            if isinstance(control_status, str) and control_status:
                state = control_status
            control_occurred_at = latest_control.get("occurredAt")
            if isinstance(control_occurred_at, str) and control_occurred_at:
                updated_at = control_occurred_at
                if control_status in {"cancelled", "expired"}:
                    completed_at = control_occurred_at

        waiting_on: list[dict[str, object]] = []
        active_operations: list[str] = []
        if controls and state in {"paused_operator", "paused_budget", "paused_policy", "paused_callback_delivery"}:
            latest_control = controls[-1]
            wait_kind_by_state = {
                "paused_operator": "operator",
                "paused_budget": "budget",
                "paused_policy": "policy",
                "paused_callback_delivery": "callback_delivery",
            }
            waiting: dict[str, object] = {"kind": wait_kind_by_state[state]}
            reason = latest_control.get("reason")
            if isinstance(reason, str) and reason:
                waiting["reason"] = reason
            waiting_on.append(waiting)
        if state not in {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}:
            for operation_id in sorted(self._callbacks_by_operation_id):
                submissions = self._callbacks_by_operation_id[operation_id]
                if not submissions:
                    continue
                submission = submissions[-1]
                if submission.run_id != run_id:
                    continue
                waiting: dict[str, object] = {
                    "kind": "callback",
                    "operationId": submission.operation_id,
                }
                if submission.node_id is not None:
                    waiting["nodeId"] = submission.node_id
                if submission.attempt_id is not None:
                    waiting["attemptId"] = submission.attempt_id
                waiting_on.append(waiting)
                active_operations.append(submission.operation_id)
            if waiting_on and state == "running":
                state = "waiting_callback"

        payload: dict[str, object] = {
            "runId": run_id,
            "state": state,
            "releaseId": release_id,
            "lastCursor": f"{run_id}:{last_sequence}",
            "startedAt": started_at,
            "updatedAt": updated_at,
            "completedAt": completed_at,
            "waitingOn": waiting_on,
            "activeOperations": active_operations,
        }
        if include_ok:
            return {"ok": True, **payload}
        return payload

    def _run_control_response(
        self,
        run_id: str,
        operation: str,
        events: tuple[dict[str, object], ...],
        payload: Mapping[str, object],
        occurred_at: str,
    ) -> ServerResponse:
        control_states = {
            "cancel_run": "cancelled",
            "pause_run": "paused_operator",
            "resume_run": "resuming",
            "expire_run": "expired",
        }
        terminal_control_states = {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
            "policy_stopped",
        }
        status = control_states[operation]
        existing = self._run_controls_by_run_id.get(run_id, ())
        if existing:
            latest_control = existing[-1]
            current_status = latest_control.get("status")
            if isinstance(current_status, str) and current_status in terminal_control_states:
                if status == current_status:
                    return ServerResponse.json(
                        200,
                        {
                            "ok": True,
                            "runId": run_id,
                            "status": current_status,
                            "reason": latest_control.get("reason"),
                            "lastCursor": latest_control.get("lastCursor"),
                            "duplicate": True,
                        },
                    )
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_status,
                        "error": f"run {run_id} is terminal with state {current_status}",
                    },
                )
        reason = payload.get("reason")
        if reason is not None:
            reason = _validate_non_empty_string("run control request", "reason", reason)
        record = _freeze_json_value("run control record", "record", {
            "operation": operation,
            "status": status,
            "reason": reason,
            "occurredAt": occurred_at,
            "lastCursor": f"{run_id}:{self._last_event_sequence(events)}",
        })
        self._run_controls_by_run_id[run_id] = (*existing, record)
        return ServerResponse.json(
            202,
            {
                "ok": True,
                "runId": run_id,
                "status": status,
                "reason": reason,
                "lastCursor": record["lastCursor"],
            },
        )

    def _callback_delivery_control_response(
        self,
        delivery_id: str,
        operation: str,
        payload: Mapping[str, object],
        requested_at: str,
    ) -> ServerResponse:
        delivery_id = _validate_non_empty_string("callback delivery control request", "delivery_id", delivery_id)
        operator = _validate_non_empty_string(
            "callback delivery control request",
            "operator",
            payload.get("operator", payload.get("operatorPrincipal", "")),
        )
        reason = _validate_non_empty_string(
            "callback delivery control request",
            "reason",
            payload.get("reason", ""),
        )
        status = (
            "redrive_requested"
            if operation == "redrive_callback_delivery"
            else "dead_letter_requested"
        )
        record = _freeze_json_value("callback delivery control record", "record", {
            "deliveryId": delivery_id,
            "operator": operator,
            "reason": reason,
            "requestedAt": requested_at,
            "status": status,
        })
        if operation == "redrive_callback_delivery":
            existing = self._callback_delivery_redrives.get(delivery_id, ())
            self._callback_delivery_redrives[delivery_id] = (*existing, record)
        else:
            existing = self._callback_delivery_dead_letter_moves.get(delivery_id, ())
            if existing:
                first = existing[0]
                return ServerResponse.json(
                    200,
                    {
                        "ok": True,
                        "deliveryId": delivery_id,
                        "operator": first.get("operator"),
                        "reason": first.get("reason"),
                        "status": first.get("status"),
                        "requestedAt": first.get("requestedAt"),
                        "duplicate": True,
                    },
                )
            self._callback_delivery_dead_letter_moves[delivery_id] = (*existing, record)
        return ServerResponse.json(
            202,
            {
                "ok": True,
                "deliveryId": delivery_id,
                "operator": operator,
                "reason": reason,
                "status": status,
            },
        )

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
            if last_cursor == f"{run_id}:0":
                replay_after_sequence = 0
            elif last_cursor not in sequence_by_cursor:
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
            else:
                replay_after_sequence = sequence_by_cursor[last_cursor]

        replayed_events = []
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > replay_after_sequence:
                replayed_events.append(_response_json_object(event))

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
        record = _freeze_json_value("detach record", "record", {
            "clientId": client_id,
            "reason": reason,
            "detachedAt": detached_at,
            "lastCursor": last_cursor,
        })
        existing = self._detachments_by_run_id.get(run_id, ())
        for detached in existing:
            if detached.get("clientId") == client_id:
                return ServerResponse.json(
                    200,
                    {
                        "ok": True,
                        "runId": run_id,
                        "clientId": client_id,
                        "reason": detached.get("reason"),
                        "status": "detached",
                        "lastCursor": detached.get("lastCursor"),
                        "detachedAt": detached.get("detachedAt"),
                        "duplicate": True,
                    },
                )
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

    def _subscription_replay(
        self,
        subscription: ServerEventSubscription,
        events: tuple[dict[str, object], ...],
    ) -> list[dict[str, object]] | ServerResponse:
        replay_after_sequence = 0
        sequence_by_cursor = {
            f"{subscription.run_id}:{sequence}": sequence
            for event in events
            if isinstance((metadata := event.get("metadata")), Mapping)
            and isinstance((sequence := metadata.get("sequence")), int)
            and not isinstance(sequence, bool)
        }
        if subscription.replay_from_cursor is not None:
            if subscription.replay_from_cursor == f"{subscription.run_id}:0":
                replay_after_sequence = 0
            elif subscription.replay_from_cursor not in sequence_by_cursor:
                last_sequence = self._last_event_sequence(events)
                nearest_cursor = (
                    f"{subscription.run_id}:{min(sequence_by_cursor.values())}" if sequence_by_cursor else None
                )
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "error": "CursorExpired",
                        "runId": subscription.run_id,
                        "requestedCursor": subscription.replay_from_cursor,
                        "nearestAvailableCursor": nearest_cursor,
                        "lastCursor": f"{subscription.run_id}:{last_sequence}",
                        "lastSequence": last_sequence,
                    },
                )
            else:
                replay_after_sequence = sequence_by_cursor[subscription.replay_from_cursor]

        replayed_events: list[dict[str, object]] = []
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= replay_after_sequence:
                continue
            if self._event_matches_subscription_filter(event, subscription.event_filter):
                replayed_events.append(_response_json_object(event))
        return replayed_events

    def _event_matches_subscription_filter(self, event: Mapping[str, object], event_filter: Mapping[str, object]) -> bool:
        event_kind = event.get("kind")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if not self._event_payload_field_matches(payload, "visibility", event_filter.get("visibility")):
            return False
        node_filter = event_filter.get("node_ids", event_filter.get("nodeIds"))
        if node_filter is not None:
            allowed_nodes = _validate_string_sequence("server event subscription", "event_filter.node_ids", node_filter)
            node_matches = False
            for source in (event, payload):
                for field_name in ("nodeId", "node_id"):
                    value = source.get(field_name)
                    if isinstance(value, str) and value in allowed_nodes:
                        node_matches = True
            if not node_matches:
                return False
        operation_filter = event_filter.get("operation_ids", event_filter.get("operationIds"))
        if operation_filter is not None:
            allowed_operations = _validate_string_sequence(
                "server event subscription",
                "event_filter.operation_ids",
                operation_filter,
            )
            operation_matches = False
            for source in (event, payload):
                for field_name in ("operationId", "operation_id"):
                    value = source.get(field_name)
                    if isinstance(value, str) and value in allowed_operations:
                        operation_matches = True
            if not operation_matches:
                return False
        severity_min = event_filter.get("severity_min", event_filter.get("severityMin"))
        if severity_min is None:
            pass
        else:
            severity_min_text = _validate_non_empty_string(
                "server event subscription",
                "event_filter.severity_min",
                severity_min,
            )
            minimum_rank = SERVER_EVENT_SEVERITY_RANKS.get(severity_min_text)
            event_severity = payload.get("severity")
            if minimum_rank is None or not isinstance(event_severity, str):
                return False
            event_rank = SERVER_EVENT_SEVERITY_RANKS.get(event_severity)
            if event_rank is None or event_rank < minimum_rank:
                return False
        include_terminal_events = event_filter.get(
            "include_terminal_events",
            event_filter.get("includeTerminalEvents", True),
        )
        if not isinstance(include_terminal_events, bool):
            raise ValueError("server event subscription event_filter.include_terminal_events must be a boolean")
        if include_terminal_events and isinstance(event_kind, str) and event_kind in SERVER_TERMINAL_EVENT_KINDS:
            return True
        types = event_filter.get("types")
        if types is None:
            return True
        allowed_types = _validate_string_sequence("server event subscription", "event_filter.types", types)
        return isinstance(event_kind, str) and event_kind in allowed_types

    def _event_payload_field_matches(
        self,
        payload: Mapping[str, object],
        field_name: str,
        allowed_values: object,
    ) -> bool:
        if allowed_values is None:
            return True
        allowed = _validate_string_sequence(
            "server event subscription",
            f"event_filter.{field_name}",
            allowed_values,
        )
        value = payload.get(field_name)
        return isinstance(value, str) and value in allowed

    def _subscription_for(self, run_id: str, subscription_id: str) -> ServerEventSubscription | None:
        for subscription in self._subscriptions_by_run_id.get(run_id, ()):
            if subscription.subscription_id == subscription_id:
                return subscription
        return None

    def _ack_event_response(
        self,
        run_id: str,
        subscription_id: str,
        events: tuple[dict[str, object], ...],
        payload: Mapping[str, object],
        acknowledged_at: str,
    ) -> ServerResponse:
        event_id = payload.get("event_id", payload.get("eventId"))
        cursor = payload.get("cursor")
        if event_id is None and cursor is None:
            raise ValueError("ack request requires event_id or cursor")
        event_id_text = (
            _validate_non_empty_string("ack request", "event_id", event_id)
            if event_id is not None
            else None
        )
        cursor_text = (
            _validate_non_empty_string("ack request", "cursor", cursor)
            if cursor is not None
            else None
        )
        matched_event = self._find_event_for_ack(run_id, events, event_id_text, cursor_text)
        if matched_event is None:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": "acknowledged event not found in retained run events",
                    "runId": run_id,
                    "subscriptionId": subscription_id,
                    "eventId": event_id_text,
                    "cursor": cursor_text,
                },
            )
        metadata = matched_event.get("metadata")
        assert isinstance(metadata, Mapping)
        event_id_text = str(metadata.get("eventId", event_id_text or ""))
        sequence = metadata.get("sequence")
        cursor_text = f"{run_id}:{sequence}" if isinstance(sequence, int) and not isinstance(sequence, bool) else cursor_text
        record = _freeze_json_value("event ack record", "record", {
            "eventId": event_id_text,
            "cursor": cursor_text,
            "acknowledgedAt": acknowledged_at,
        })
        key = (run_id, subscription_id)
        existing = self._acks_by_subscription.get(key, ())
        for ack in existing:
            if ack.get("eventId") == event_id_text and ack.get("cursor") == cursor_text:
                return ServerResponse.json(
                    200,
                    {
                        "ok": True,
                        "runId": run_id,
                        "subscriptionId": subscription_id,
                        "eventId": event_id_text,
                        "cursor": cursor_text,
                        "status": "duplicate",
                        "duplicate": True,
                        "acknowledgedAt": ack.get("acknowledgedAt"),
                    },
                )
        self._acks_by_subscription[key] = (*existing, record)
        return ServerResponse.json(
            202,
            {
                "ok": True,
                "runId": run_id,
                "subscriptionId": subscription_id,
                "eventId": event_id_text,
                "cursor": cursor_text,
                "status": "acknowledged",
            },
        )

    def _find_event_for_ack(
        self,
        run_id: str,
        events: tuple[dict[str, object], ...],
        event_id: str | None,
        cursor: str | None,
    ) -> dict[str, object] | None:
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            event_cursor = f"{run_id}:{sequence}" if isinstance(sequence, int) and not isinstance(sequence, bool) else None
            metadata_event_id = metadata.get("eventId")
            if event_id is not None and metadata_event_id == event_id:
                return event
            if cursor is not None and event_cursor == cursor:
                return event
        return None

    def _callback_registration_replay(
        self,
        registration: ServerCallbackRegistration,
    ) -> tuple[list[dict[str, object]], str | None] | ServerResponse:
        if registration.scope != "run":
            return ([], None)
        events = self._events_by_run_id.get(registration.scope_id)
        if events is None:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": f"run event stream not found for callback registration scope {registration.scope_id!r}",
                },
            )
        subscription = ServerEventSubscription(
            subscription_id=registration.subscription_id,
            run_id=registration.scope_id,
            event_filter=registration.event_filter,
            delivery=registration.delivery,
            failure_policy=registration.failure_policy,
            replay_from_cursor=registration.replay_from_cursor,
            created_at=registration.created_at,
        )
        replay = self._subscription_replay(subscription, events)
        if isinstance(replay, ServerResponse):
            return replay
        return (replay, f"{registration.scope_id}:{self._last_event_sequence(events)}")


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
    "ServerCallbackRegistration",
    "ServerEndpoint",
    "ServerEventSubscription",
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
