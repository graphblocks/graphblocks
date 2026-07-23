from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from threading import Condition, get_ident, TIMEOUT_MAX
from time import monotonic, time
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.parse import quote, unquote

from .admission import (
    AdmissionError,
    AdmissionIdempotencyConflictError,
    AdmissionQueueFullError,
    AdmissionTicket,
    AdmissionTicketQueue,
)
from .application_event import (
    APPLICATION_COMMAND_KINDS,
    APPLICATION_PROTOCOL_EVENT_KINDS,
    ApplicationCommand,
    ApplicationCommandKind,
    ApplicationCommandMetadata,
    ApplicationEvent,
    ApplicationEventMetadata,
    ApplicationProtocolError,
    ApplicationProtocolEvent,
    ApplicationProtocolEventKind,
    ApplicationProtocolEventMetadata,
    ApplicationProtocolLog,
)
from .canonical import canonical_dumps, canonical_hash
from .compiler import compile_graph
from .policy import PrincipalRef
from .runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
    RuntimeCheckpoint,
    RuntimeRegistry,
    stdlib_registry,
)
from .url_validation import validate_webhook_url


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
VALID_CALLBACK_SUBSCRIPTION_STATUSES = frozenset({"active", "paused", "expired", "revoked"})
VALID_CALLBACK_FAILURE_POLICIES = frozenset({
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
})
MANDATORY_CALLBACK_FAILURE_POLICIES = frozenset({"pause_run_on_failure", "fail_run_on_failure"})
VALID_CALLBACK_DELIVERY_KINDS = frozenset({
    "webhook",
    "websocket",
    "sse",
    "push_notification",
    "email",
    "local_callback",
})
ORDER_CAPABLE_CALLBACK_TARGETS = frozenset({"webhook", "websocket", "sse"})
VALID_EVENT_VISIBILITIES = frozenset({"client", "operator", "internal", "audit_only"})
VALID_ATTACH_CAPABILITIES = frozenset({
    "assistant_drafts",
    "retractions",
    "artifact_preview",
    "patch_preview",
    "approval",
    "review",
    "budget_extension",
    "background_notifications",
    "interrupt_resume",
})
VALID_WEBHOOK_SIGNING_ALGORITHMS = frozenset({"hmac-sha256", "ed25519"})
VALID_SERVER_CALLBACK_DELIVERY_STATUSES = frozenset({
    "pending",
    "delivered",
    "acknowledged",
    "failed",
    "cancelled",
})
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
    "RunExpired",
})
MAX_SERVER_REQUEST_JSON_DEPTH = 64
MAX_RUN_CURSOR_SEQUENCE = (1 << 64) - 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{owner} {field_name} must not be empty")
    return stripped


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    text = _validate_non_empty_string(owner, field_name, value)
    if value != text:
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return text


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


def _validate_iso_datetime(owner: str, field_name: str, value: object) -> str:
    timestamp = _validate_exact_non_empty_string(owner, field_name, value)
    if len(timestamp) <= 19 or timestamp[10] != "T":
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    suffix_start = 19
    if timestamp[suffix_start] == ".":
        suffix_start += 1
        fraction_start = suffix_start
        while suffix_start < len(timestamp) and timestamp[suffix_start].isdigit():
            suffix_start += 1
        if suffix_start == fraction_start:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    timezone_suffix = timestamp[suffix_start:]
    if timestamp.endswith("Z"):
        normalized = f"{timestamp[:-1]}+00:00"
    elif (
        len(timezone_suffix) != 6
        or timezone_suffix[0] not in {"+", "-"}
        or timezone_suffix[3] != ":"
        or not timezone_suffix[1:3].isdigit()
        or not timezone_suffix[4:6].isdigit()
        or int(timezone_suffix[1:3]) > 23
        or int(timezone_suffix[4:6]) > 59
    ):
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    else:
        normalized = timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    return timestamp


def _validate_run_cursor(owner: str, field_name: str, run_id: str, value: object) -> str:
    cursor = _validate_exact_non_empty_string(owner, field_name, value)
    prefix = f"{run_id}:"
    if not cursor.startswith(prefix):
        raise ValueError(f"{owner} {field_name} must belong to run {run_id!r}")
    sequence_text = cursor[len(prefix) :]
    if not sequence_text.isascii() or not sequence_text.isdecimal():
        raise ValueError(
            f"{owner} {field_name} must use '<run_id>:<sequence>' with a non-negative integer sequence"
        )
    if int(sequence_text) > MAX_RUN_CURSOR_SEQUENCE:
        raise ValueError(
            f"{owner} {field_name} sequence must be at most {MAX_RUN_CURSOR_SEQUENCE}"
        )
    return cursor


def _server_request_json_body(request: ServerRequest, owner: str) -> object:
    try:
        encoded = request.body.decode("utf-8") or "{}"
        depth = 0
        in_string = False
        escaped = False
        for character in encoded:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
            elif character in "]}":
                depth -= 1

        def reject_duplicate_keys(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON object key {key!r}")
                result[key] = value
            return result

        decoded = json.loads(
            encoded,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            object_pairs_hook=reject_duplicate_keys,
        )
        pending: list[tuple[object, int]] = [(decoded, 0)]
        while pending:
            value, value_depth = pending.pop()
            if isinstance(value, dict):
                container_depth = value_depth + 1
                if container_depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
                pending.extend((item, container_depth) for item in value.values())
            elif isinstance(value, list):
                container_depth = value_depth + 1
                if container_depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
                pending.extend((item, container_depth) for item in value)
        canonical_dumps(decoded)
        return decoded
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError(f"{owner} body must be valid JSON") from error


def _validate_callback_subscription_scope(value: object) -> str:
    scope = _validate_non_empty_string("server callback registration", "scope", value)
    if value != scope or scope not in VALID_CALLBACK_SUBSCRIPTION_SCOPES:
        raise ValueError(
            "server callback registration scope must be one of run, conversation, project, tenant, or deployment"
        )
    return scope


def _validate_callback_failure_policy(value: object) -> str:
    failure_policy = _validate_exact_non_empty_string("server subscription", "failure_policy", value)
    if failure_policy not in VALID_CALLBACK_FAILURE_POLICIES:
        raise ValueError(
            "server subscription failure_policy must be one of best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure"
        )
    return failure_policy


def _validate_callback_subscription_status(owner: str, value: object) -> str:
    status = _validate_exact_non_empty_string(owner, "status", value)
    if status not in VALID_CALLBACK_SUBSCRIPTION_STATUSES:
        raise ValueError(f"{owner} status must be one of active, paused, expired, or revoked")
    return status


def _has_callback_dead_letter_config(config: Mapping[str, object], delivery: Mapping[str, object]) -> bool:
    dead_letter = (
        config.get("deadLetterPolicy")
        or config.get("dead_letter_policy")
        or config.get("deadLetterRef")
        or config.get("dead_letter_ref")
        or delivery.get("deadLetterPolicy")
        or delivery.get("dead_letter_policy")
        or delivery.get("deadLetterRef")
        or delivery.get("dead_letter_ref")
    )
    fallback = (
        config.get("fallbackPolicy")
        or config.get("fallback_policy")
        or config.get("fallbackRef")
        or config.get("fallback_ref")
        or delivery.get("fallbackPolicy")
        or delivery.get("fallback_policy")
        or delivery.get("fallbackRef")
        or delivery.get("fallback_ref")
    )
    return (
        isinstance(dead_letter, Mapping)
        or (isinstance(dead_letter, str) and bool(dead_letter.strip()))
        or isinstance(fallback, Mapping)
        or (isinstance(fallback, str) and bool(fallback.strip()))
    )


def _validate_mandatory_callback_policy(
    owner: str,
    config: Mapping[str, object],
    delivery: Mapping[str, object],
    failure_policy: str,
) -> None:
    mandatory = config.get("mandatory") is True or delivery.get("mandatory") is True
    if mandatory and failure_policy == "best_effort" and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(
            f"{owner} mandatory delivery requires retry, dead-letter, pause-run, or fail-run failure policy"
        )
    explicitly_retrying = (
        failure_policy == "retry_then_dead_letter"
        and ("failurePolicy" in config or "failure_policy" in config)
    )
    if explicitly_retrying and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(f"{owner} retrying callback failure policy requires dead-letter or fallback behavior")
    if failure_policy in MANDATORY_CALLBACK_FAILURE_POLICIES and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(f"{owner} mandatory callback failure policy requires dead-letter or fallback behavior")


def _validate_callback_not_authoritative(owner: str, config: Mapping[str, object]) -> None:
    authoritative_for = _server_alias_value(
        config,
        owner,
        "authoritative_for",
        "authoritativeFor",
    )
    source_of_truth = _server_alias_value(
        config,
        owner,
        "source_of_truth",
        "sourceOfTruth",
    )
    if source_of_truth is True or authoritative_for:
        raise ValueError(f"{owner} callback delivery must not be used as the source of truth")


def _webhook_url_is_unsafe(url: str) -> bool:
    return not validate_webhook_url(
        url,
        allowed_schemes=frozenset({"https"}),
    ).allowed


def _validate_callback_delivery_target(owner: str, delivery: Mapping[str, object]) -> None:
    raw_delivery_kind = delivery.get("kind", "")
    delivery_kind = _validate_non_empty_string(owner, "delivery.kind", raw_delivery_kind)
    if raw_delivery_kind != delivery_kind or delivery_kind not in VALID_CALLBACK_DELIVERY_KINDS:
        raise ValueError(
            f"{owner} delivery.kind must be one of webhook, websocket, sse, push_notification, email, or local_callback"
        )
    ordering = delivery.get("ordering")
    if (
        isinstance(ordering, Mapping)
        and ordering.get("mode") == "ordered"
        and delivery_kind not in ORDER_CAPABLE_CALLBACK_TARGETS
    ):
        raise ValueError(f"{owner} delivery.ordering requests ordered delivery on an unsupported target")
    if delivery_kind != "webhook":
        return
    raw_method = delivery.get("method", "POST")
    method = _validate_non_empty_string(owner, "delivery.method", raw_method)
    if raw_method != method or method != "POST":
        raise ValueError(f"{owner} delivery.method must be POST for webhook delivery")
    raw_url = delivery.get("url", "")
    url = _validate_non_empty_string(owner, "delivery.url", raw_url)
    if raw_url != url or _webhook_url_is_unsafe(url):
        raise ValueError(f"{owner} delivery.url is unsafe or forbidden by default egress policy")
    signing = delivery.get("signing")
    if not isinstance(signing, Mapping):
        raise ValueError(f"{owner} delivery.signing must be a mapping for webhook delivery")
    raw_algorithm = signing.get("algorithm", "")
    algorithm = _validate_non_empty_string(owner, "delivery.signing.algorithm", raw_algorithm)
    if raw_algorithm != algorithm or algorithm not in VALID_WEBHOOK_SIGNING_ALGORITHMS:
        raise ValueError(f"{owner} delivery.signing.algorithm must be one of hmac-sha256 or ed25519")
    _validate_non_empty_string(
        owner,
        "delivery.signing.secret_ref",
        _server_alias_value(
            signing,
            owner,
            "secret_ref",
            "secretRef",
            "",
        ),
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
        key_text = _validate_exact_non_empty_string(owner, f"{field_name} key", key)
        if not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} values must be strings")
        normalized_key = key_text.lower() if lowercase_keys else key_text
        if normalized_key in normalized:
            raise ValueError(f"{owner} {field_name} contains duplicate key {normalized_key!r}")
        normalized[normalized_key] = item
    return MappingProxyType(normalized)


def _validate_http_headers(owner: str, value: object) -> MappingProxyType[str, str]:
    headers = _validate_string_mapping(owner, "headers", value, lowercase_keys=True)
    token_punctuation = frozenset("!#$%&'*+-.^_`|~")
    for key, item in headers.items():
        if not all(
            character.isascii()
            and (character.isalnum() or character in token_punctuation)
            for character in key
        ):
            raise ValueError(f"{owner} headers key must be an HTTP token")
        if any(
            (ord(character) < 32 and character != "\t") or ord(character) == 127
            for character in item
        ):
            raise ValueError(f"{owner} headers values must not contain control characters")
    return headers


def _validate_http_message_framing(
    owner: str,
    headers: Mapping[str, str],
    body: bytes,
) -> None:
    content_length = headers.get("content-length")
    if content_length is not None and "transfer-encoding" in headers:
        raise ValueError(
            f"{owner} must not combine content-length and transfer-encoding"
        )
    if content_length is None:
        return
    if not content_length.isascii() or not content_length.isdecimal():
        raise ValueError(f"{owner} content-length must use ASCII decimal digits")
    if int(content_length) != len(body):
        raise ValueError(f"{owner} content-length must match body length")


def _validate_string_sequence(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{owner} {field_name} must be a sequence")
    normalized = tuple(
        _validate_non_empty_string(
            owner,
            field_name,
            item,
        )
        for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} {field_name} must not contain duplicates")
    return tuple(sorted(normalized))


def _freeze_json_value(
    owner: str,
    field_name: str,
    value: object,
    *,
    _depth: int = 0,
    _active_containers: set[int] | None = None,
) -> object:
    if _depth > MAX_SERVER_REQUEST_JSON_DEPTH:
        raise ValueError(f"{owner} {field_name} nesting exceeds the server JSON limit")
    if _active_containers is None:
        _active_containers = set()
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError(f"{owner} {field_name} must not be recursive")
        _active_containers.add(container_id)
        frozen: dict[str, object] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{owner} {field_name} keys must be non-empty strings")
                frozen[key] = _freeze_json_value(
                    owner,
                    f"{field_name}.{key}",
                    item,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
            return MappingProxyType(frozen)
        finally:
            _active_containers.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError(f"{owner} {field_name} must not be recursive")
        _active_containers.add(container_id)
        try:
            return tuple(
                _freeze_json_value(
                    owner,
                    field_name,
                    item,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
                for item in value
            )
        finally:
            _active_containers.remove(container_id)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {field_name} must be finite")
        return value
    raise ValueError(f"{owner} {field_name} must be a JSON value")


def _validate_server_event_filter(owner: str, event_filter: Mapping[str, object]) -> None:
    for raw_values, field_name in (
        (event_filter.get("types"), "types"),
        (
            _server_alias_value(
                event_filter,
                owner,
                "node_ids",
                "nodeIds",
            ),
            "node_ids",
        ),
        (
            _server_alias_value(
                event_filter,
                owner,
                "operation_ids",
                "operationIds",
            ),
            "operation_ids",
        ),
    ):
        if raw_values is not None:
            _validate_string_sequence(owner, f"event_filter.{field_name}", raw_values)
            if any(value != value.strip() for value in raw_values):  # type: ignore[union-attr]
                raise ValueError(
                    f"{owner} event_filter.{field_name} values must not contain surrounding whitespace"
                )

    visibility = event_filter.get("visibility")
    if visibility is not None:
        visibility_values = _validate_string_sequence(owner, "event_filter.visibility", visibility)
        if any(value != value.strip() for value in visibility) or any(
            value not in VALID_EVENT_VISIBILITIES for value in visibility_values
        ):
            raise ValueError(
                f"{owner} event_filter.visibility must contain only client, operator, internal, or audit_only"
            )

    severity_min = _server_alias_value(
        event_filter,
        owner,
        "severity_min",
        "severityMin",
    )
    if severity_min is not None:
        severity_min_text = _validate_non_empty_string(
            owner,
            "event_filter.severity_min",
            severity_min,
        )
        if severity_min != severity_min_text or severity_min_text not in SERVER_EVENT_SEVERITY_RANKS:
            raise ValueError(f"{owner} event_filter.severity_min is invalid")

    include_terminal_events = _server_alias_value(
        event_filter,
        owner,
        "include_terminal_events",
        "includeTerminalEvents",
    )
    if include_terminal_events is not None and not isinstance(include_terminal_events, bool):
        raise ValueError(f"{owner} event_filter.include_terminal_events must be a boolean")


def _authorized_callback_visibility(principal: PrincipalRef | None) -> tuple[str, ...]:
    allowed = ["client"]
    roles = set(principal.roles if principal is not None else ())
    if "operator" in roles:
        allowed.append("operator")
    if "internal" in roles:
        allowed.append("internal")
    if "audit" in roles or "auditor" in roles:
        allowed.append("audit_only")
    return tuple(allowed)


def _event_visibility(event: Mapping[str, object]) -> str | None:
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    for source in (metadata, event, payload):
        visibility = source.get("visibility")
        if visibility is None:
            continue
        if isinstance(visibility, str) and visibility in VALID_EVENT_VISIBILITIES:
            return visibility
        return None
    return "client"


def _event_visible_to_principal(event: Mapping[str, object], principal: PrincipalRef | None) -> bool:
    visibility = _event_visibility(event)
    return visibility is not None and visibility in _authorized_callback_visibility(principal)


def _constrain_event_filter_visibility(
    event_filter: Mapping[str, object],
    principal: PrincipalRef | None,
) -> Mapping[str, object]:
    allowed_visibility = _authorized_callback_visibility(principal)
    visibility = event_filter.get("visibility")
    if visibility is None:
        constrained_visibility = allowed_visibility
    else:
        requested_visibility = _validate_string_sequence(
            "server event subscription",
            "event_filter.visibility",
            visibility,
        )
        allowed = set(allowed_visibility)
        constrained_visibility = tuple(value for value in requested_visibility if value in allowed)
    constrained = dict(event_filter)
    constrained["visibility"] = list(constrained_visibility)
    return MappingProxyType(constrained)


def _principal_response_payload(owner: PrincipalRef) -> dict[str, object]:
    return {
        "principalId": owner.principal_id,
        "tenantId": owner.tenant_id,
        "groups": list(owner.groups),
        "roles": list(owner.roles),
        "attributes": _thaw_json_value(
            _freeze_json_value(
                "server principal",
                "attributes",
                owner.attributes,
            )
        ),
    }


def _principal_matches_owner(principal: PrincipalRef | None, owner: PrincipalRef) -> bool:
    if principal is None:
        return False
    return principal.principal_id == owner.principal_id and principal.tenant_id == owner.tenant_id


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
        object.__setattr__(
            self,
            "endpoints",
            tuple(
                sorted(
                    endpoints,
                    key=lambda endpoint: (
                        endpoint.method,
                        sum(
                            part.startswith("{") and part.endswith("}")
                            for part in endpoint.path.strip("/").split("/")
                            if part
                        ),
                        endpoint.path,
                        endpoint.transport,
                        endpoint.operation,
                        endpoint.auth_required,
                    ),
                )
            ),
        )

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

    def match(
        self,
        method: str,
        path: str,
        *,
        transport: ServerTransport | None = None,
    ) -> ServerRouteMatch:
        normalized_method = _validate_non_empty_string("server route lookup", "method", method).upper()
        path = _validate_route_path("server route lookup", path)
        normalized_transport = _validate_transport(transport) if transport is not None else None
        path_parts = [part for part in path.strip("/").split("/") if part]
        matches: list[ServerRouteMatch] = []
        for endpoint in self.endpoints:
            if endpoint.method != normalized_method:
                continue
            if normalized_transport is not None and endpoint.transport != normalized_transport:
                continue
            endpoint_parts = [part for part in endpoint.path.strip("/").split("/") if part]
            if endpoint.path == path:
                matches.append(ServerRouteMatch(endpoint))
                continue
            if len(endpoint_parts) != len(path_parts):
                continue
            path_params: dict[str, str] = {}
            for template_part, path_part in zip(endpoint_parts, path_parts, strict=True):
                if template_part.startswith("{") and template_part.endswith("}"):
                    path_params[template_part[1:-1]] = unquote(path_part)
                    continue
                if template_part != path_part:
                    break
            else:
                matches.append(ServerRouteMatch(endpoint, path_params))
        if matches:
            if normalized_transport is None:
                matching_transports = {match.endpoint.transport for match in matches}
                if len(matching_transports) > 1:
                    raise ValueError(
                        f"server route {normalized_method} {path!r} is ambiguous across transports; "
                        "specify transport"
                    )
            return min(
                matches,
                key=lambda match: (
                    match.endpoint.path != path,
                    sum(
                        part.startswith("{") and part.endswith("}")
                        for part in match.endpoint.path.strip("/").split("/")
                        if part
                    ),
                    match.endpoint.path,
                    match.endpoint.transport,
                    match.endpoint.operation,
                ),
            )
        raise ServerRouteNotFoundError(method, path)

    def lookup(
        self,
        method: str,
        path: str,
        *,
        transport: ServerTransport | None = None,
    ) -> ServerEndpoint:
        return self.match(method, path, transport=transport).endpoint

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
            _validate_http_headers("server auth request", self.headers),
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
            _validate_http_headers("server request", self.headers),
        )
        object.__setattr__(self, "query", _validate_string_mapping("server request", "query", self.query))
        object.__setattr__(self, "cookies", _validate_string_mapping("server request", "cookies", self.cookies))
        if not isinstance(self.body, (bytes, bytearray, memoryview)):
            raise ValueError("server request body must be bytes")
        object.__setattr__(self, "body", bytes(self.body))
        _validate_http_message_framing("server request", self.headers, self.body)
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
            _validate_http_headers("server response", self.headers),
        )
        if not isinstance(self.body, (bytes, bytearray, memoryview)):
            raise ValueError("server response body must be bytes")
        object.__setattr__(self, "body", bytes(self.body))
        _validate_http_message_framing("server response", self.headers, self.body)

    def read(self) -> bytes:
        return self.body

    @classmethod
    def json(cls, status_code: int, payload: Mapping[str, object]) -> ServerResponse:
        if not isinstance(payload, Mapping):
            raise ValueError("server response JSON payload must be a mapping")
        payload_copy = _response_json_object(_freeze_json_value("server response JSON", "payload", payload))
        canonical_dumps(payload_copy)
        return cls(
            status_code=status_code,
            headers={"content-type": "application/json"},
            body=json.dumps(payload_copy, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8"),
        )


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
    dict[str, tuple[ServerAsyncCallbackRejection, ...]]
):
    """Keep diagnostic callback receipts useful without accepting unbounded input."""

    _MAX_OPERATION_IDS = 1_024
    _MAX_REJECTIONS_PER_OPERATION = 32

    def __setitem__(
        self,
        operation_id: str,
        rejections: tuple[ServerAsyncCallbackRejection, ...],
    ) -> None:
        if operation_id not in self and len(self) >= self._MAX_OPERATION_IDS:
            del self[next(iter(self))]
        super().__setitem__(
            operation_id,
            tuple(rejections[-self._MAX_REJECTIONS_PER_OPERATION :]),
        )


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
            object.__setattr__(
                self,
                field_name,
                _validate_exact_non_empty_string(
                    "server callback delivery result",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("server callback delivery result sequence must be a non-negative integer")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("server callback delivery result attempt must be a positive integer")
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
                "pending, delivered, acknowledged, failed, or cancelled"
            )
        if self.status_code is not None and (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or self.status_code < 100
            or self.status_code > 599
        ):
            raise ValueError("server callback delivery result status_code must be a valid HTTP status")
        if self.delivered_at is not None:
            object.__setattr__(
                self,
                "delivered_at",
                _validate_iso_datetime(
                    "server callback delivery result",
                    "delivered_at",
                    self.delivered_at,
                ),
            )
        if self.last_error is not None:
            object.__setattr__(
                self,
                "last_error",
                _validate_exact_non_empty_string(
                    "server callback delivery result",
                    "last_error",
                    self.last_error,
                ),
            )
        if self.status in {"delivered", "acknowledged"}:
            if self.status_code is None or self.delivered_at is None:
                raise ValueError(
                    "successful server callback delivery result requires status_code and delivered_at"
                )
            if self.last_error is not None:
                raise ValueError("successful server callback delivery result must not have last_error")
        if self.status in {"failed", "cancelled"} and self.last_error is None:
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


def _server_alias_value(
    body: Mapping[str, object],
    owner: str,
    snake: str,
    camel: str,
    default: object = None,
) -> object:
    if snake in body and camel in body:
        raise ValueError(
            f"{owner} {snake} must not contain multiple field aliases"
        )
    return body.get(snake, body.get(camel, default))


def _optional_callback_string(body: Mapping[str, object], snake: str, camel: str) -> str | None:
    value = _callback_alias_value(body, snake, camel)
    if value is None:
        return None
    return _validate_exact_non_empty_string("server async callback", snake, value)


def _callback_idempotency_key(body: Mapping[str, object], headers: Mapping[str, str]) -> object:
    graphblocks_header_value = headers.get("graphblocks-idempotency-key")
    legacy_header_value = headers.get("idempotency-key")
    if (
        graphblocks_header_value is not None
        and legacy_header_value is not None
        and graphblocks_header_value != legacy_header_value
    ):
        raise ValueError("server async callback idempotency_key header values must not conflict")
    header_value = graphblocks_header_value if graphblocks_header_value is not None else legacy_header_value
    body_value = _callback_alias_value(body, "idempotency_key", "idempotencyKey")
    if body_value is not None and header_value is not None and body_value != header_value:
        raise ValueError("server async callback idempotency_key body/header values must not conflict")
    if body_value is not None:
        return body_value
    if header_value is not None:
        return header_value
    return ""


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


class ServerAuthHook(Protocol):
    def authorize(self, request: ServerAuthRequest) -> ServerAuthDecision:
        ...


class ServerAsyncCallbackResumeAdmissionHook(Protocol):
    def admit(
        self,
        submission: ServerAsyncCallbackSubmission,
        checkpoint: RuntimeCheckpoint,
    ) -> Mapping[str, object]:
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
        scheme, separator, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not separator:
            return ServerAuthDecision(False, reason_codes=("auth.missing_bearer_token",))
        token = token.strip()
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
class _AcceptedRunExecution:
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


@dataclass(slots=True)
class GraphBlocksServerApp:
    route_manifest: ServerRouteManifest = field(default_factory=default_server_route_manifest)
    auth_hook: ServerAuthHook | None = None
    health: ServerHealth = field(default_factory=lambda: ServerHealth("graphblocks-api"))
    registry: RuntimeRegistry = field(default_factory=stdlib_registry)
    max_async_callback_payload_bytes: int = 262144
    require_async_callback_authentication: bool = False
    anti_enumerate_async_callbacks: bool = False
    defer_accepted_runs: bool = False
    accepted_run_executor: Executor | None = None
    admission_ticket_queue: AdmissionTicketQueue | None = None
    admission_clock: Callable[[], int] = field(
        default=lambda: int(time() * 1_000),
        repr=False,
    )
    async_callback_resume_admission_hook: ServerAsyncCallbackResumeAdmissionHook | None = None
    callback_delivery_hook: ServerCallbackDeliveryHook | None = None
    _events_by_run_id: dict[str, tuple[Mapping[str, object], ...]] = field(default_factory=dict, init=False, repr=False)
    _callbacks_by_operation_id: dict[str, tuple[ServerAsyncCallbackSubmission, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _async_callback_rejections_by_operation_id: dict[str, tuple[ServerAsyncCallbackRejection, ...]] = field(
        default_factory=_BoundedAsyncCallbackRejectionHistory,
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
    _subscription_registration_condition: Condition = field(
        default_factory=Condition,
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
    _pending_callback_registration_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _incomplete_callback_registration_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _callback_registration_condition: Condition = field(
        default_factory=Condition,
        init=False,
        repr=False,
    )
    _callback_delivery_results_by_subscription_id: dict[
        str,
        tuple[ServerCallbackDeliveryResult, ...],
    ] = field(
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
    _pending_accepted_runs_by_run_id: dict[str, Mapping[str, object]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _admission_ticket_ids_by_run_id: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _admitting_accepted_run_ids: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _accepted_run_condition: Condition = field(
        default_factory=Condition,
        init=False,
        repr=False,
    )
    _advancing_accepted_runs_by_run_id: dict[str, CancellationToken] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _accepted_run_results_by_run_id: dict[str, Mapping[str, object]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _accepted_run_executions_by_run_id: dict[str, _AcceptedRunExecution] = field(
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
        if not isinstance(self.require_async_callback_authentication, bool):
            raise ValueError("server require_async_callback_authentication must be a boolean")
        if not isinstance(self.anti_enumerate_async_callbacks, bool):
            raise ValueError("server anti_enumerate_async_callbacks must be a boolean")
        if not isinstance(self.defer_accepted_runs, bool):
            raise ValueError("server defer_accepted_runs must be a boolean")
        if self.admission_ticket_queue is not None and not isinstance(
            self.admission_ticket_queue,
            AdmissionTicketQueue,
        ):
            raise ValueError("server admission_ticket_queue must be AdmissionTicketQueue or null")
        if not callable(self.admission_clock):
            raise ValueError("server admission_clock must be callable")

    def handle(self, request: ServerRequest) -> ServerResponse:
        try:
            requested_transport: ServerTransport = "http"
            if request.headers.get("upgrade", "").casefold() == "websocket":
                requested_transport = "websocket"
            elif "text/event-stream" in request.headers.get("accept", "").casefold():
                requested_transport = "sse"
            try:
                route_match = self.route_manifest.match(
                    request.method,
                    request.path,
                    transport=requested_transport,
                )
            except ServerRouteNotFoundError:
                # SSE endpoints are also commonly polled over a plain HTTP GET.
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

        auth_decision = ServerAuthDecision(True)
        if self.auth_hook is not None:
            try:
                hook_decision = self.auth_hook.authorize(
                    ServerAuthRequest(
                        route=route,
                        headers=request.headers,
                        query=request.query,
                        cookies=request.cookies,
                        requested_at=request.requested_at,
                    )
                )
            except Exception:
                return ServerResponse.json(
                    401,
                    {
                        "ok": False,
                        "reasonCodes": ["auth.hook_error"],
                    },
                )
            if not isinstance(hook_decision, ServerAuthDecision):
                return ServerResponse.json(
                    401,
                    {
                        "ok": False,
                        "reasonCodes": ["auth.invalid_decision"],
                    },
                )
            auth_decision = hook_decision
            if not auth_decision.allowed:
                if route.operation == "submit_async_callback":
                    try:
                        submission = ServerAsyncCallbackSubmission.from_request(
                            operation_id=route_match.path_params.get("operation_id", ""),
                            request=request,
                        )
                        rejection = ServerAsyncCallbackRejection.authentication_failed(submission)
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                return ServerResponse.json(
                    401,
                    {
                        "ok": False,
                        "reasonCodes": list(auth_decision.reason_codes),
                    },
                )
        if (
            route.operation == "submit_async_callback"
            and self.require_async_callback_authentication
            and auth_decision.principal is None
        ):
            return ServerResponse.json(
                401,
                {
                    "ok": False,
                    "reasonCodes": ["auth.callback_authentication_required"],
                },
            )

        if route.operation == "health":
            return ServerResponse.json(200, self.health.to_payload())
        if route.operation == "list_runs":
            try:
                runs = [
                    self._run_status_payload(run_id, events, include_ok=False)
                    for run_id, events in sorted(self._events_by_run_id.items())
                ]
            except (TypeError, ValueError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
            return ServerResponse.json(200, {"ok": True, "runs": runs})
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
                payload = _server_request_json_body(request, "run control request")
                if not isinstance(payload, Mapping):
                    raise ValueError("run control request body must be a JSON object")
                with self._accepted_run_condition:
                    return self._run_control_response(
                        run_id,
                        route.operation,
                        events,
                        payload,
                        request.requested_at or _utc_now_iso(),
                        auth_decision.principal,
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
            try:
                return ServerResponse.json(200, self._run_status_payload(run_id, events))
            except (TypeError, ValueError) as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
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
                payload = _server_request_json_body(request, "attach request")
                if not isinstance(payload, Mapping):
                    raise ValueError("attach request body must be a JSON object")
                return self._attach_to_run_response(run_id, events, payload, auth_decision.principal)
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
                payload = _server_request_json_body(request, "detach request")
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
                with self._subscription_registration_condition:
                    existing = self._subscriptions_by_run_id.get(run_id, ())
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
                                    f"subscription {subscription.subscription_id!r} "
                                    f"already exists for run {run_id!r}"
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
            with self._subscription_registration_condition:
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
                with self._subscription_registration_condition:
                    subscription = self._subscription_for(run_id, subscription_id)
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
                    return self._ack_event_response(
                        run_id,
                        subscription_id,
                        subscription,
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
                with self._callback_registration_condition:
                    registration = ServerCallbackRegistration.from_request(
                        request=request,
                        ordinal=len(
                            set(self._callback_registrations).union(
                                self._pending_callback_registration_ids
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
                    if registration.subscription_id in self._pending_callback_registration_ids:
                        return ServerResponse.json(
                            409,
                            {
                                "ok": False,
                                "subscriptionId": registration.subscription_id,
                                "state": "pending",
                                "error": f"callback registration {registration.subscription_id!r} already exists",
                            },
                        )
                    existing = self._callback_registrations.get(registration.subscription_id)
                    resuming_incomplete_registration = False
                    if existing is not None:
                        retry_registration = replace(registration, created_at=existing.created_at)
                        if (
                            registration.subscription_id
                            not in self._incomplete_callback_registration_ids
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
                    self._pending_callback_registration_ids.add(registration.subscription_id)

                replay_ready = False
                try:
                    replay = self._callback_registration_replay(registration)
                    if isinstance(replay, ServerResponse):
                        return replay
                    replayed_events, last_cursor = replay
                    replay_ready = True
                finally:
                    if not replay_ready:
                        with self._callback_registration_condition:
                            self._pending_callback_registration_ids.discard(registration.subscription_id)

                with self._callback_registration_condition:
                    if not resuming_incomplete_registration:
                        self._callback_registrations[registration.subscription_id] = registration

                delivery_results: tuple[ServerCallbackDeliveryResult, ...] = ()
                if self.callback_delivery_hook is not None and registration.delivery.get("kind") == "webhook":
                    with self._callback_registration_condition:
                        self._incomplete_callback_registration_ids.add(registration.subscription_id)
                        delivered = list(
                            self._callback_delivery_results_by_subscription_id.get(
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
                                delivery_result = self.callback_delivery_hook.deliver(registration, event)
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
                            delivered.append(delivery_result)
                            completed_event_ids.add(delivery_result.event_id)
                            with self._callback_registration_condition:
                                self._callback_delivery_results_by_subscription_id[
                                    registration.subscription_id
                                ] = tuple(delivered)
                        delivery_results = tuple(delivered)
                        with self._callback_registration_condition:
                            self._incomplete_callback_registration_ids.discard(registration.subscription_id)
                    finally:
                        with self._callback_registration_condition:
                            self._pending_callback_registration_ids.discard(registration.subscription_id)
                else:
                    with self._callback_registration_condition:
                        self._pending_callback_registration_ids.discard(registration.subscription_id)
                return ServerResponse.json(
                    200 if resuming_incomplete_registration else 201,
                    registration.response_payload(replayed_events, last_cursor, delivery_results),
                )
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
            with self._callback_registration_condition:
                registration = self._callback_registrations.get(subscription_id)
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
                payload = _server_request_json_body(request, "callback delivery control request")
                if not isinstance(payload, Mapping):
                    raise ValueError("callback delivery control request body must be a JSON object")
                return self._callback_delivery_control_response(
                    delivery_id,
                    route.operation,
                    payload,
                    request.requested_at or _utc_now_iso(),
                    auth_decision.principal,
                )
            except PermissionError as error:
                return ServerResponse.json(
                    403,
                    {
                        "ok": False,
                        "error": str(error),
                    },
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
                payload_size_bytes = len(canonical_dumps(_thaw_json_value(submission.payload)).encode("utf-8"))
                if payload_size_bytes > self.max_async_callback_payload_bytes:
                    rejection = ServerAsyncCallbackRejection.payload_too_large(submission)
                    self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                        *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                        rejection,
                    )
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
                for previous in self._callbacks_by_operation_id.get(
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
                if submission.run_id is not None and submission.run_id not in self._events_by_run_id:
                    rejection = ServerAsyncCallbackRejection.unknown_run(submission)
                    self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                        *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                        rejection,
                    )
                    if self.anti_enumerate_async_callbacks:
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
                    self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                        *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                        rejection,
                    )
                    self._append_async_callback_diagnostic_event(
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
                    self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                        *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                        rejection,
                    )
                    self._append_async_callback_diagnostic_event(
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
                    run_status = self._run_status_payload(
                        submission.run_id,
                        self._events_by_run_id[submission.run_id],
                        include_ok=False,
                    )
                    state = run_status.get("state")
                    if state in {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}:
                        rejection = ServerAsyncCallbackRejection.terminal_run(submission, state)
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                self._accepted_run_condition.acquire()
                callback_state_locked = True
                if submission.run_id is not None:
                    current_events = self._events_by_run_id.get(submission.run_id)
                    if current_events is None:
                        rejection = ServerAsyncCallbackRejection.unknown_run(
                            submission
                        )
                        self._async_callback_rejections_by_operation_id[
                            submission.operation_id
                        ] = (
                            *self._async_callback_rejections_by_operation_id.get(
                                submission.operation_id,
                                (),
                            ),
                            rejection,
                        )
                        if self.anti_enumerate_async_callbacks:
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
                    current_status = self._run_status_payload(
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
                        self._async_callback_rejections_by_operation_id[
                            submission.operation_id
                        ] = (
                            *self._async_callback_rejections_by_operation_id.get(
                                submission.operation_id,
                                (),
                            ),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                    pending_execution = self._accepted_run_executions_by_run_id.get(
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
                        self._async_callback_rejections_by_operation_id[
                            submission.operation_id
                        ] = (
                            *self._async_callback_rejections_by_operation_id.get(
                                submission.operation_id,
                                (),
                            ),
                            rejection,
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
                existing = self._callbacks_by_operation_id.get(submission.operation_id, ())
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
                            self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                                *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                                rejection,
                            )
                            self._append_async_callback_diagnostic_event(
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
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
                            "ExternalCallbackRejected",
                            submission,
                            "scope_mismatch",
                        )
                        return ServerResponse.json(409, payload)
                    if previous.attempt_id != submission.attempt_id:
                        rejection = ServerAsyncCallbackRejection.stale_attempt(submission)
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                    self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                        *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                        rejection,
                    )
                    self._append_async_callback_diagnostic_event(
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
                resumable_execution = (
                    self._accepted_run_executions_by_run_id.get(
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
                        self._async_callback_rejections_by_operation_id[
                            submission.operation_id
                        ] = (
                            *self._async_callback_rejections_by_operation_id.get(
                                submission.operation_id,
                                (),
                            ),
                            rejection,
                        )
                        self._append_async_callback_diagnostic_event(
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
                            self._async_callback_rejections_by_operation_id[
                                submission.operation_id
                            ] = (
                                *self._async_callback_rejections_by_operation_id.get(
                                    submission.operation_id,
                                    (),
                                ),
                                rejection,
                            )
                            self._append_async_callback_diagnostic_event(
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
                    waiting_events = self._events_by_run_id.get(
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
                    pending_run = self._pending_accepted_runs_by_run_id.get(
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
                    admission_hook = self.async_callback_resume_admission_hook
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
                        return ServerResponse.json(
                            403,
                            {
                                "ok": False,
                                "operationId": submission.operation_id,
                                "runId": submission.run_id,
                                "error": f"async callback resume admission failed: {error}",
                            },
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
                self._callbacks_by_operation_id[submission.operation_id] = (*existing, submission)
                self._append_async_callback_diagnostic_event(
                    "ExternalCallbackReceived",
                    submission,
                    None,
                )
                if (
                    resumable_execution is not None
                    and resumable_execution.checkpoint is not None
                    and resumable_execution.callback_receipt is not None
                    and not resumable_execution.resume_dispatch_pending
                    and self.accepted_run_executor is not None
                ):
                    resumable_execution.resume_dispatch_pending = True
                    try:
                        resume_future = self.accepted_run_executor.submit(
                            self.advance_accepted_run,
                            submission.run_id,
                        )
                        resumable_execution.resume_future = resume_future
                        resume_future.add_done_callback(
                            lambda completed_future, dispatched_run_id=submission.run_id: (
                                self._accepted_run_resume_dispatch_done(
                                    str(dispatched_run_id),
                                    completed_future,
                                )
                            )
                        )
                    except RuntimeError as error:
                        resumable_execution.resume_dispatch_pending = False
                        paused_record = _freeze_json_value(
                            "run control record",
                            "record",
                            {
                                "operation": "resume_run",
                                "status": "paused_callback_delivery",
                                "reason": (
                                    "accepted run executor rejected callback resume: "
                                    f"{error}"
                                ),
                                "occurredAt": submission.received_at,
                                "lastCursor": (
                                    f"{submission.run_id}:"
                                    f"{self._last_event_sequence(self._events_by_run_id[submission.run_id])}"
                                ),
                            },
                        )
                        self._run_controls_by_run_id[submission.run_id] = (
                            *self._run_controls_by_run_id.get(
                                submission.run_id,
                                (),
                            ),
                            paused_record,
                        )
                        self._accepted_run_condition.notify_all()
                        paused_payload = submission.response_payload()
                        paused_payload["status"] = "paused_callback_delivery"
                        return ServerResponse.json(202, paused_payload)
                    self._accepted_run_condition.notify_all()
                elif (
                    resumable_execution is not None
                    and resumable_execution.checkpoint is not None
                    and resumable_execution.callback_receipt is not None
                    and self.accepted_run_executor is None
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
                                f"{self._last_event_sequence(self._events_by_run_id[submission.run_id])}"
                            ),
                        },
                    )
                    self._run_controls_by_run_id[submission.run_id] = (
                        *self._run_controls_by_run_id.get(
                            submission.run_id,
                            (),
                        ),
                        paused_record,
                    )
                    self._accepted_run_condition.notify_all()
                    paused_payload = submission.response_payload()
                    paused_payload["status"] = "paused_callback_delivery"
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
                        self._async_callback_rejections_by_operation_id[submission.operation_id] = (
                            *self._async_callback_rejections_by_operation_id.get(submission.operation_id, ()),
                            rejection,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
            finally:
                if callback_state_locked:
                    self._accepted_run_condition.release()
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
            try:
                cursor = request.query.get("cursor")
                if cursor is not None:
                    cursor = _validate_run_cursor("application events", "cursor", run_id, cursor)
                sequence_by_cursor: dict[str, int] = {}
                last_sequence = 0
                for event in events:
                    metadata = event.get("metadata")
                    if not isinstance(metadata, Mapping):
                        continue
                    sequence = metadata.get("sequence")
                    if not isinstance(sequence, int) or isinstance(sequence, bool):
                        raise ValueError("application events sequence must be an integer")
                    if sequence < 0:
                        raise ValueError("application events sequence must be non-negative")
                    event_cursor = f"{run_id}:{sequence}"
                    sequence_by_cursor[event_cursor] = sequence
                    if sequence > last_sequence:
                        last_sequence = sequence
                replay_after_sequence = 0
                if cursor is not None:
                    if cursor == f"{run_id}:0":
                        replay_after_sequence = 0
                    elif cursor not in sequence_by_cursor:
                        nearest_cursor = f"{run_id}:{min(sequence_by_cursor.values())}" if sequence_by_cursor else None
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
                                "runStatus": self._run_status_payload(run_id, events, include_ok=False),
                            },
                        )
                    else:
                        replay_after_sequence = sequence_by_cursor[cursor]
            except ValueError as error:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": str(error),
                    },
                )
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "replayFromCursor": cursor,
                    "lastCursor": f"{run_id}:{last_sequence}",
                    "events": [
                        _response_json_object(event)
                        for event in events
                        if isinstance((metadata := event.get("metadata")), Mapping)
                        and isinstance((sequence := metadata.get("sequence")), int)
                        and not isinstance(sequence, bool)
                        and sequence >= 0
                        and sequence > replay_after_sequence
                        and _event_visible_to_principal(event, auth_decision.principal)
                    ],
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
                    sequence = metadata.get("sequence")
                    if not isinstance(sequence, int) or isinstance(sequence, bool):
                        return ServerResponse.json(
                            400,
                            {
                                "ok": False,
                                "error": "application stream sequence must be an integer",
                            },
                        )
                    if sequence < 0:
                        return ServerResponse.json(
                            400,
                            {
                                "ok": False,
                                "error": "application stream sequence must be non-negative",
                            },
                        )
                    if sequence > last_sequence:
                        last_sequence = sequence
            visible_events = [
                _response_json_object(event)
                for event in events
                if _event_visible_to_principal(event, auth_decision.principal)
            ]
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "runId": run_id,
                    "stream": {
                        "transport": "websocket",
                        "status": "accepted",
                        "cursor": f"{run_id}:{last_sequence}",
                        "eventCount": len(visible_events),
                    },
                    "events": visible_events,
                },
            )
        if route.operation == "invoke_graph":
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
                run_id = _validate_exact_non_empty_string(
                    "run request",
                    "runId",
                    _server_alias_value(
                        payload,
                        "run request",
                        "run_id",
                        "runId",
                        "run-000001",
                    ),
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
                if run_id in self._events_by_run_id:
                    existing_ticket_id = self._admission_ticket_ids_by_run_id.get(
                        run_id
                    )
                    if (
                        existing_ticket_id is not None
                        and self.admission_ticket_queue is not None
                        and response_mode in {"accepted", "background"}
                    ):
                        existing_ticket = self.admission_ticket_queue.get(
                            existing_ticket_id
                        )
                        owner_id = (
                            auth_decision.principal.principal_id
                            if auth_decision.principal is not None
                            else "anonymous"
                        )
                        if (
                            existing_ticket.request_id == request_id
                            and existing_ticket.owner_id == owner_id
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
                response_id = _validate_exact_non_empty_string(
                    "run request",
                    "responseId",
                    _server_alias_value(
                        payload,
                        "run request",
                        "response_id",
                        "responseId",
                        "response-000001",
                    ),
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
                    self.admission_ticket_queue is not None
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

                block_catalog = self.registry.compilation_catalog()
                plan = (
                    compile_graph(graph, block_catalog=block_catalog)
                    if block_catalog is not None
                    else compile_graph(graph)
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
                accepted_run_execution = _AcceptedRunExecution(
                    runtime=InProcessRuntime(
                        self.registry,
                        cancellation_token=accepted_run_cancellation_token,
                        journal_factory=lambda _run_id: accepted_run_journal,
                    ),
                    cancellation_token=accepted_run_cancellation_token,
                    journal=accepted_run_journal,
                )
                deferred = (
                    ticketed_admission
                    or (
                        self.defer_accepted_runs
                        and response_mode in {"accepted", "background"}
                    )
                )
                completion = None
                admission_ticket: AdmissionTicket | None = None
                if ticketed_admission:
                    assert self.admission_ticket_queue is not None
                    admission_now_ms = self.admission_clock()
                    self.promote_admission_tickets(now_ms=admission_now_ms)
                    owner_id = (
                        auth_decision.principal.principal_id
                        if auth_decision.principal is not None
                        else "anonymous"
                    )
                    with self._accepted_run_condition:
                        if (
                            run_id in self._events_by_run_id
                            or run_id in self._admitting_accepted_run_ids
                        ):
                            return ServerResponse.json(
                                409,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "error": f"run {run_id!r} already exists",
                                },
                            )
                        try:
                            submission = self.admission_ticket_queue.submit(
                                run_id,
                                request_id,
                                owner_id,
                                now_ms=admission_now_ms,
                                units=admission_units,
                            )
                        except AdmissionQueueFullError as error:
                            return ServerResponse.json(
                                429,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "limiterId": error.limiter_id,
                                    "error": str(error),
                                },
                            )
                        except AdmissionIdempotencyConflictError as error:
                            return ServerResponse.json(
                                409,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "error": str(error),
                                },
                            )
                        admission_ticket = submission.ticket
                        self._events_by_run_id[run_id] = ()
                        self._pending_accepted_runs_by_run_id[run_id] = pending_run
                        self._accepted_run_executions_by_run_id[
                            run_id
                        ] = accepted_run_execution
                        self._admission_ticket_ids_by_run_id[
                            run_id
                        ] = admission_ticket.ticket_id
                        self._accepted_run_condition.notify_all()
                    if admission_ticket.state == "admitted":
                        self._dispatch_admitted_tickets((admission_ticket,))
                elif deferred and self.accepted_run_executor is not None:
                    try:
                        executor_probe = self.accepted_run_executor.submit(
                            get_ident
                        )
                    except RuntimeError as error:
                        return ServerResponse.json(
                            503,
                            {
                                "ok": False,
                                "runId": run_id,
                                "error": (
                                    "accepted run executor rejected work: "
                                    f"{error}"
                                ),
                            },
                        )
                    if executor_probe.done():
                        try:
                            executor_thread_id = executor_probe.result()
                        except Exception as error:
                            return ServerResponse.json(
                                503,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "error": (
                                        "accepted run executor rejected work: "
                                        f"{error}"
                                    ),
                                },
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
                    with self._accepted_run_condition:
                        if (
                            run_id in self._events_by_run_id
                            or run_id in self._admitting_accepted_run_ids
                        ):
                            return ServerResponse.json(
                                409,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "error": f"run {run_id!r} already exists",
                                },
                            )
                        self._admitting_accepted_run_ids.add(run_id)
                    try:
                        self.accepted_run_executor.submit(
                            self.advance_accepted_run,
                            run_id,
                        )
                    except RuntimeError as error:
                        with self._accepted_run_condition:
                            self._admitting_accepted_run_ids.discard(run_id)
                            self._accepted_run_condition.notify_all()
                        return ServerResponse.json(
                            503,
                            {
                                "ok": False,
                                "runId": run_id,
                                "error": (
                                    "accepted run executor rejected work: "
                                    f"{error}"
                                ),
                            },
                        )
                    with self._accepted_run_condition:
                        assert frozen_start_event is not None
                        self._events_by_run_id[run_id] = (frozen_start_event,)
                        self._pending_accepted_runs_by_run_id[run_id] = pending_run
                        self._accepted_run_executions_by_run_id[
                            run_id
                        ] = accepted_run_execution
                        self._admitting_accepted_run_ids.discard(run_id)
                        self._accepted_run_condition.notify_all()
                else:
                    with self._accepted_run_condition:
                        if (
                            run_id in self._events_by_run_id
                            or run_id in self._admitting_accepted_run_ids
                        ):
                            return ServerResponse.json(
                                409,
                                {
                                    "ok": False,
                                    "runId": run_id,
                                    "error": f"run {run_id!r} already exists",
                                },
                            )
                        assert frozen_start_event is not None
                        self._events_by_run_id[run_id] = (frozen_start_event,)
                        self._pending_accepted_runs_by_run_id[run_id] = pending_run
                        self._accepted_run_executions_by_run_id[
                            run_id
                        ] = accepted_run_execution
                if not deferred:
                    completion = self.advance_accepted_run(
                        run_id,
                        completed_at=occurred_at,
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
                        accepted_payload["admissionTicket"] = (
                            self.admission_ticket_queue.get(
                                admission_ticket.ticket_id
                            ).contract()
                            if self.admission_ticket_queue is not None
                            else admission_ticket.contract()
                        )
                    return ServerResponse.json(
                        202,
                        accepted_payload,
                    )
                assert completion is not None
                return ServerResponse.json(
                    200,
                    {
                        "runId": run_id,
                        "status": completion["status"],
                        "outputs": completion["outputs"],
                        "events": [
                            _response_json_object(event)
                            for event in self._events_by_run_id[run_id]
                        ],
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

    def pending_accepted_run_ids(self) -> tuple[str, ...]:
        with self._accepted_run_condition:
            return tuple(sorted(self._pending_accepted_runs_by_run_id))

    def promote_admission_tickets(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Run one non-blocking admission maintenance pass.

        An external server loop may call this at the next retry deadline.  A
        process without an executor can claim admitted runs explicitly through
        ``advance_accepted_run``.
        """

        if self.admission_ticket_queue is None:
            return ()
        maintenance_now_ms = (
            self.admission_clock() if now_ms is None else now_ms
        )
        expired = self.admission_ticket_queue.expire(now_ms=maintenance_now_ms)
        expired_at = (
            datetime.fromtimestamp(maintenance_now_ms / 1_000, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        for ticket in expired:
            with self._accepted_run_condition:
                events = self._events_by_run_id.get(ticket.run_id)
                if (
                    events is None
                    or ticket.run_id
                    not in self._pending_accepted_runs_by_run_id
                ):
                    continue
                self._run_control_response(
                    ticket.run_id,
                    "expire_run",
                    events,
                    {"reason": "admission ticket TTL expired"},
                    expired_at,
                    None,
                )
        promoted = self.admission_ticket_queue.promote(
            now_ms=maintenance_now_ms
        )
        self._dispatch_admitted_tickets(promoted)
        return tuple(ticket.contract() for ticket in promoted)

    def _dispatch_admitted_tickets(
        self,
        tickets: tuple[AdmissionTicket, ...],
    ) -> None:
        if self.accepted_run_executor is None:
            return
        for ticket in tickets:
            if ticket.state != "admitted":
                continue
            try:
                self.accepted_run_executor.submit(
                    self.advance_accepted_run,
                    ticket.run_id,
                )
            except RuntimeError:
                # The admitted ticket remains claimable by a later worker pass;
                # no RunStarted event is published before an actual claim.
                continue

    def wait_for_accepted_run(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        run_id = _validate_exact_non_empty_string(
            "server accepted run worker",
            "run_id",
            run_id,
        )
        timeout_seconds: float | None = None
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError(
                    "server accepted run worker timeout must be a finite non-negative number"
                )
            try:
                timeout_seconds = float(timeout)
            except OverflowError:
                raise ValueError(
                    "server accepted run worker timeout must be a finite non-negative number"
                ) from None
            if (
                not math.isfinite(timeout_seconds)
                or timeout_seconds < 0
                or timeout_seconds > TIMEOUT_MAX
            ):
                raise ValueError(
                    "server accepted run worker timeout must be a finite non-negative number"
                )
        deadline = None if timeout_seconds is None else monotonic() + timeout_seconds
        with self._accepted_run_condition:
            while True:
                stored_result = self._accepted_run_results_by_run_id.get(run_id)
                if stored_result is not None:
                    result = _thaw_json_value(stored_result)
                    assert isinstance(result, dict)
                    return result
                controls = self._run_controls_by_run_id.get(run_id, ())
                latest_status = controls[-1].get("status") if controls else None
                if latest_status in {
                    "paused_operator",
                    "paused_budget",
                    "paused_policy",
                    "paused_callback_delivery",
                }:
                    events = self._events_by_run_id[run_id]
                    return {
                        "runId": run_id,
                        "status": latest_status,
                        "outputs": {},
                        "lastCursor": (
                            f"{run_id}:{self._last_event_sequence(events)}"
                        ),
                        "duplicate": False,
                    }
                execution = self._accepted_run_executions_by_run_id.get(run_id)
                if (
                    execution is not None
                    and execution.checkpoint is not None
                    and execution.callback_receipt is None
                ):
                    events = self._events_by_run_id[run_id]
                    outputs = _thaw_json_value(
                        execution.checkpoint.output_values
                    )
                    assert isinstance(outputs, dict)
                    return {
                        "runId": run_id,
                        "status": "waiting_callback",
                        "outputs": outputs,
                        "lastCursor": (
                            f"{run_id}:{self._last_event_sequence(events)}"
                        ),
                        "duplicate": False,
                    }
                if (
                    run_id not in self._pending_accepted_runs_by_run_id
                    and run_id not in self._advancing_accepted_runs_by_run_id
                ):
                    raise ValueError(f"accepted run worker {run_id!r} not found")
                remaining = (
                    None if deadline is None else max(0.0, deadline - monotonic())
                )
                if remaining == 0.0:
                    raise TimeoutError()
                self._accepted_run_condition.wait(remaining)

    def _accepted_run_resume_dispatch_done(
        self,
        run_id: str,
        future: Future[object],
    ) -> None:
        with self._accepted_run_condition:
            execution = self._accepted_run_executions_by_run_id.get(run_id)
            if execution is None or execution.resume_future is not future:
                return
            if run_id in self._advancing_accepted_runs_by_run_id:
                return
            execution.resume_future = None
            execution.resume_dispatch_pending = False
            if future.cancelled():
                reason = "accepted run callback resume dispatch was cancelled before claim"
            else:
                error = future.exception()
                if error is None:
                    return
                reason = f"accepted run callback resume failed before claim: {error}"
            events = self._events_by_run_id.get(run_id, ())
            pause_at = _utc_now_iso()
            pause_datetime = datetime.fromisoformat(
                f"{pause_at[:-1]}+00:00"
            ).astimezone(timezone.utc)
            timestamp_floors: list[str] = []
            if events:
                latest_event_metadata = events[-1].get("metadata", {})
                latest_event_at = (
                    latest_event_metadata.get("occurredAt")
                    if isinstance(latest_event_metadata, Mapping)
                    else None
                )
                timestamp_floors.append(
                    _validate_iso_datetime(
                        "accepted run callback resume dispatch",
                        "latest_event_at",
                        latest_event_at,
                    )
                )
            controls = self._run_controls_by_run_id.get(run_id, ())
            if controls:
                timestamp_floors.append(
                    _validate_iso_datetime(
                        "accepted run callback resume dispatch",
                        "latest_control_at",
                        controls[-1].get("occurredAt"),
                    )
                )
            for timestamp_floor in timestamp_floors:
                floor_datetime = datetime.fromisoformat(
                    f"{timestamp_floor[:-1]}+00:00"
                    if timestamp_floor.endswith("Z")
                    else timestamp_floor
                ).astimezone(timezone.utc)
                if pause_datetime < floor_datetime:
                    pause_at = timestamp_floor
                    pause_datetime = floor_datetime
            paused_record = _freeze_json_value(
                "run control record",
                "record",
                {
                    "operation": "resume_run",
                    "status": "paused_callback_delivery",
                    "reason": reason,
                    "occurredAt": pause_at,
                    "lastCursor": (
                        f"{run_id}:{self._last_event_sequence(events)}"
                    ),
                },
            )
            self._run_controls_by_run_id[run_id] = (
                *self._run_controls_by_run_id.get(run_id, ()),
                paused_record,
            )
            self._accepted_run_condition.notify_all()

    def advance_accepted_run(
        self,
        run_id: str,
        *,
        completed_at: str | None = None,
    ) -> dict[str, object]:
        run_id = _validate_exact_non_empty_string(
            "server pending accepted run",
            "run_id",
            run_id,
        )
        if completed_at is not None:
            completed_at = _validate_iso_datetime(
                "server pending accepted run",
                "completed_at",
                completed_at,
            )
        admission_ticket_id = self._admission_ticket_ids_by_run_id.get(run_id)
        admission_fencing_token: int | None = None
        with self._accepted_run_condition:
            while run_id in self._admitting_accepted_run_ids:
                self._accepted_run_condition.wait()
            while run_id in self._advancing_accepted_runs_by_run_id:
                stored_result = self._accepted_run_results_by_run_id.get(run_id)
                if stored_result is not None:
                    duplicate_result = _thaw_json_value(stored_result)
                    assert isinstance(duplicate_result, dict)
                    duplicate_result["duplicate"] = True
                    return duplicate_result
                self._accepted_run_condition.wait()

            stored_result = self._accepted_run_results_by_run_id.get(run_id)
            if stored_result is not None:
                duplicate_result = _thaw_json_value(stored_result)
                assert isinstance(duplicate_result, dict)
                duplicate_result["duplicate"] = True
                return duplicate_result

            pending = self._pending_accepted_runs_by_run_id.get(run_id)
            if pending is None:
                events = self._events_by_run_id.get(run_id)
                if events is None:
                    raise ValueError(f"pending accepted run {run_id!r} not found")
                terminal_states = {
                    "RunSucceeded": "succeeded",
                    "RunCompleted": "completed",
                    "RunFailed": "failed",
                    "RunCancelled": "cancelled",
                    "RunExpired": "expired",
                    "RunPolicyStopped": "policy_stopped",
                }
                terminal_event = next(
                    (
                        event
                        for event in reversed(events)
                        if event.get("kind") in terminal_states
                    ),
                    None,
                )
                if terminal_event is None:
                    raise ValueError(f"run {run_id!r} is not pending or terminal")
                terminal_payload = terminal_event.get("payload", {})
                terminal_metadata = terminal_event.get("metadata", {})
                outputs = (
                    terminal_payload.get("outputs", {})
                    if isinstance(terminal_payload, Mapping)
                    else {}
                )
                cursor = (
                    terminal_metadata.get("cursor")
                    if isinstance(terminal_metadata, Mapping)
                    else None
                )
                return {
                    "runId": run_id,
                    "status": terminal_states[str(terminal_event["kind"])],
                    "outputs": _thaw_json_value(outputs),
                    "lastCursor": cursor,
                    "duplicate": True,
                }

            if admission_ticket_id is not None:
                if self.admission_ticket_queue is None:
                    raise ValueError(
                        f"run {run_id!r} has an admission ticket but no admission queue"
                    )
                admission_ticket = self.admission_ticket_queue.get(
                    admission_ticket_id
                )
                if admission_ticket.state == "queued":
                    raise ValueError(
                        f"pending accepted run {run_id!r} is queued for admission"
                    )
                if admission_ticket.state not in {"admitted", "running"}:
                    raise ValueError(
                        f"pending accepted run {run_id!r} admission ticket is {admission_ticket.state}"
                    )
                admission_fencing_token = admission_ticket.fencing_token
                if admission_fencing_token is None:
                    raise ValueError(
                        f"pending accepted run {run_id!r} has no admission fencing token"
                    )
                if admission_ticket.state == "admitted":
                    admission_ticket = self.admission_ticket_queue.mark_running(
                        admission_ticket_id,
                        admission_fencing_token,
                        now_ms=self.admission_clock(),
                    )
                if not self._events_by_run_id.get(run_id, ()):
                    start_response_id = pending.get("responseId")
                    start_release_id = pending.get("releaseId")
                    start_policy_snapshot_id = pending.get("policySnapshotId")
                    start_turn_id = pending.get("turnId")
                    graph_hash = pending.get("graphHash")
                    if not isinstance(start_response_id, str):
                        raise ValueError(
                            "pending accepted run responseId must be a string"
                        )
                    if not isinstance(start_release_id, str):
                        raise ValueError(
                            "pending accepted run releaseId must be a string"
                        )
                    if not isinstance(start_policy_snapshot_id, str):
                        raise ValueError(
                            "pending accepted run policySnapshotId must be a string"
                        )
                    if start_turn_id is not None and not isinstance(
                        start_turn_id,
                        str,
                    ):
                        raise ValueError(
                            "pending accepted run turnId must be a string or null"
                        )
                    if not isinstance(graph_hash, str):
                        raise ValueError(
                            "pending accepted run graphHash must be a string"
                        )
                    claimed_at = _utc_now_iso()
                    start_event = ApplicationEvent.new(
                        "RunStarted",
                        ApplicationEventMetadata(
                            event_id=f"{run_id}:run-started",
                            run_id=run_id,
                            response_id=start_response_id,
                            turn_id=start_turn_id,
                            sequence=1,
                            release_id=start_release_id,
                            policy_snapshot_id=start_policy_snapshot_id,
                            occurred_at=claimed_at,
                            cursor=f"{run_id}:1",
                        ),
                        payload={
                            "status": "running",
                            "graph_hash": graph_hash,
                            "admission_ticket_id": admission_ticket_id,
                        },
                    )
                    frozen_claim_event = _freeze_json_value(
                        "application event stream",
                        "event",
                        {
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
                        },
                    )
                    assert isinstance(frozen_claim_event, Mapping)
                    self._events_by_run_id[run_id] = (frozen_claim_event,)

            start_events = self._events_by_run_id.get(run_id, ())
            start_metadata = (
                start_events[0].get("metadata", {}) if start_events else {}
            )
            started_at = (
                start_metadata.get("occurredAt")
                if isinstance(start_metadata, Mapping)
                else None
            )
            started_at = _validate_iso_datetime(
                "server pending accepted run",
                "started_at",
                started_at,
            )
            started_datetime = datetime.fromisoformat(
                f"{started_at[:-1]}+00:00"
                if started_at.endswith("Z")
                else started_at
            ).astimezone(timezone.utc)
            if completed_at is not None:
                completed_datetime = datetime.fromisoformat(
                    f"{completed_at[:-1]}+00:00"
                    if completed_at.endswith("Z")
                    else completed_at
                ).astimezone(timezone.utc)
                if completed_datetime < started_datetime:
                    raise ValueError(
                        "pending accepted run completed_at must not be before run start"
                    )

            controls = self._run_controls_by_run_id.get(run_id, ())
            if controls:
                latest_status = controls[-1].get("status")
                if latest_status in {
                    "paused_operator",
                    "paused_budget",
                    "paused_policy",
                    "paused_callback_delivery",
                }:
                    events = self._events_by_run_id[run_id]
                    return {
                        "runId": run_id,
                        "status": latest_status,
                        "outputs": {},
                        "lastCursor": (
                            f"{run_id}:{self._last_event_sequence(events)}"
                        ),
                        "duplicate": False,
                    }

            execution = self._accepted_run_executions_by_run_id.get(run_id)
            if execution is None:
                raise ValueError(
                    f"pending accepted run {run_id!r} has no process-local execution state"
                )
            if (
                execution.checkpoint is not None
                and execution.callback_receipt is None
            ):
                events = self._events_by_run_id[run_id]
                waiting_outputs = _thaw_json_value(
                    execution.checkpoint.output_values
                )
                assert isinstance(waiting_outputs, dict)
                return {
                    "runId": run_id,
                    "status": "waiting_callback",
                    "outputs": waiting_outputs,
                    "lastCursor": (
                        f"{run_id}:{self._last_event_sequence(events)}"
                    ),
                    "duplicate": True,
                }

            graph = _thaw_json_value(pending.get("graph"))
            inputs = _thaw_json_value(pending.get("inputs"))
            if not isinstance(graph, dict) or not isinstance(inputs, dict):
                raise ValueError(
                    "pending accepted run graph and inputs must be JSON objects"
                )
            response_id = pending.get("responseId")
            release_id = pending.get("releaseId")
            policy_snapshot_id = pending.get("policySnapshotId")
            turn_id = pending.get("turnId")
            if not isinstance(response_id, str):
                raise ValueError("pending accepted run responseId must be a string")
            if not isinstance(release_id, str):
                raise ValueError("pending accepted run releaseId must be a string")
            if not isinstance(policy_snapshot_id, str):
                raise ValueError(
                    "pending accepted run policySnapshotId must be a string"
                )
            if turn_id is not None and not isinstance(turn_id, str):
                raise ValueError(
                    "pending accepted run turnId must be a string or null"
                )
            cancellation_token = execution.cancellation_token
            checkpoint = execution.checkpoint
            callback_receipt = execution.callback_receipt
            execution.resume_dispatch_pending = False
            execution.resume_future = None
            self._advancing_accepted_runs_by_run_id[run_id] = cancellation_token
            if checkpoint is not None and callback_receipt is not None:
                operation_id = checkpoint.operation.get("operation_id")
                submissions = (
                    self._callbacks_by_operation_id.get(operation_id, ())
                    if isinstance(operation_id, str)
                    else ()
                )
                resume_submission = next(
                    (
                        submission
                        for submission in reversed(submissions)
                        if submission.run_id == run_id
                    ),
                    None,
                )
                if resume_submission is None:
                    self._advancing_accepted_runs_by_run_id.pop(run_id, None)
                    raise ValueError(
                        f"pending accepted run {run_id!r} validated receipt has no accepted callback"
                    )
                self._append_async_callback_diagnostic_event(
                    "RunResuming",
                    resume_submission,
                    None,
                    occurred_at=_utc_now_iso(),
                )

        try:
            try:
                result = execution.runtime.run(
                    graph,
                    inputs,
                    run_id=run_id,
                    checkpoint=checkpoint,
                    callback_receipt=callback_receipt,
                )
                result_status = result.status
                frozen_result_outputs = _freeze_json_value(
                    "server accepted run completion",
                    "outputs",
                    dict(result.outputs),
                )
                assert isinstance(frozen_result_outputs, Mapping)
                result_outputs = _thaw_json_value(frozen_result_outputs)
                assert isinstance(result_outputs, dict)
                terminal_payload: dict[str, object] = {
                    "status": result.status,
                    "outputs": result_outputs,
                }
                if result.status == "cancelled":
                    terminal_payload = {
                        "status": result.status,
                        "reason": cancellation_token.reason or "cancelled",
                    }
                elif result.status == "failed" and result.journal.records:
                    terminal_payload.update(dict(result.journal.records[-1].payload))
            except Exception as error:
                result_status = "failed"
                result_outputs = {}
                terminal_payload = {
                    "status": "failed",
                    "outputs": {},
                    "error": str(error),
                }

            if result_status == "waiting_callback":
                assert result.checkpoint is not None
                waiting_at = completed_at or _utc_now_iso()
                waiting_datetime = datetime.fromisoformat(
                    f"{waiting_at[:-1]}+00:00"
                    if waiting_at.endswith("Z")
                    else waiting_at
                ).astimezone(timezone.utc)
                if waiting_datetime < started_datetime:
                    waiting_at = started_at
                with self._accepted_run_condition:
                    stored_result = self._accepted_run_results_by_run_id.get(
                        run_id
                    )
                    if stored_result is not None:
                        duplicate_result = _thaw_json_value(stored_result)
                        assert isinstance(duplicate_result, dict)
                        duplicate_result["duplicate"] = True
                        return duplicate_result
                    if run_id not in self._pending_accepted_runs_by_run_id:
                        raise ValueError(
                            f"pending accepted run {run_id!r} ended before checkpoint publication"
                        )
                    current_execution = self._accepted_run_executions_by_run_id.get(
                        run_id
                    )
                    if current_execution is not execution:
                        raise ValueError(
                            f"pending accepted run {run_id!r} changed execution ownership"
                        )
                    execution.checkpoint = result.checkpoint
                    execution.callback_receipt = None
                    execution.resume_dispatch_pending = False
                    events = self._events_by_run_id[run_id]
                    sequence = self._last_event_sequence(events) + 1
                    operation = result.checkpoint.operation
                    waiting_event = ApplicationEvent.new(
                        "AsyncOperationWaitingCallback",
                        ApplicationEventMetadata(
                            event_id=(
                                f"{run_id}:async-wait:{result.checkpoint.checkpoint_id}"
                            ),
                            run_id=run_id,
                            response_id=response_id,
                            turn_id=turn_id,
                            sequence=sequence,
                            release_id=release_id,
                            policy_snapshot_id=policy_snapshot_id,
                            occurred_at=waiting_at,
                            cursor=f"{run_id}:{sequence}",
                            node_id=result.checkpoint.wait_node,
                            operation_id=str(operation["operation_id"]),
                            visibility="operator",
                        ),
                        payload={
                            "status": "waiting_callback",
                            "checkpointId": result.checkpoint.checkpoint_id,
                            "operationId": operation["operation_id"],
                            "nodeId": operation["node_id"],
                            "attemptId": operation["attempt_id"],
                            "providerOperationId": operation.get(
                                "provider_operation_id"
                            ),
                            "expectedSchema": operation["expected_schema"],
                        },
                    )
                    waiting_event_payload = _freeze_json_value(
                        "application event stream",
                        "event",
                        {
                            "kind": waiting_event.kind,
                            "metadata": {
                                "eventId": waiting_event.metadata.event_id,
                                "runId": waiting_event.metadata.run_id,
                                "responseId": waiting_event.metadata.response_id,
                                "turnId": waiting_event.metadata.turn_id,
                                "sequence": waiting_event.metadata.sequence,
                                "cursor": waiting_event.metadata.cursor,
                                "releaseId": waiting_event.metadata.release_id,
                                "policySnapshotId": waiting_event.metadata.policy_snapshot_id,
                                "occurredAt": waiting_event.metadata.occurred_at,
                                "graphId": waiting_event.metadata.graph_id,
                                "nodeId": waiting_event.metadata.node_id,
                                "operationId": waiting_event.metadata.operation_id,
                                "visibility": waiting_event.metadata.visibility,
                            },
                            "payload": dict(waiting_event.payload),
                        },
                    )
                    assert isinstance(waiting_event_payload, Mapping)
                    self._events_by_run_id[run_id] = (
                        *events,
                        waiting_event_payload,
                    )
                    self._accepted_run_condition.notify_all()
                    return {
                        "runId": run_id,
                        "status": "waiting_callback",
                        "outputs": result_outputs,
                        "lastCursor": f"{run_id}:{sequence}",
                        "duplicate": False,
                    }

            terminal_at = completed_at or _utc_now_iso()
            terminal_datetime = datetime.fromisoformat(
                f"{terminal_at[:-1]}+00:00"
                if terminal_at.endswith("Z")
                else terminal_at
            ).astimezone(timezone.utc)
            if terminal_datetime < started_datetime:
                terminal_at = started_at

            with self._accepted_run_condition:
                stored_result = self._accepted_run_results_by_run_id.get(run_id)
                if stored_result is not None:
                    duplicate_result = _thaw_json_value(stored_result)
                    assert isinstance(duplicate_result, dict)
                    duplicate_result["duplicate"] = True
                    return duplicate_result

                events = self._events_by_run_id[run_id]
                latest_event_metadata = (
                    events[-1].get("metadata", {}) if events else {}
                )
                latest_event_at = (
                    latest_event_metadata.get("occurredAt")
                    if isinstance(latest_event_metadata, Mapping)
                    else None
                )
                latest_event_at = _validate_iso_datetime(
                    "server accepted run completion",
                    "latest_event_at",
                    latest_event_at,
                )
                latest_event_datetime = datetime.fromisoformat(
                    f"{latest_event_at[:-1]}+00:00"
                    if latest_event_at.endswith("Z")
                    else latest_event_at
                ).astimezone(timezone.utc)
                if terminal_datetime < latest_event_datetime:
                    terminal_at = latest_event_at
                sequence = self._last_event_sequence(events) + 1
                terminal_event = ApplicationEvent.new(
                    {
                        "succeeded": "RunSucceeded",
                        "failed": "RunFailed",
                        "cancelled": "RunCancelled",
                    }[result_status],
                    ApplicationEventMetadata(
                        event_id=f"{run_id}:run-terminal",
                        run_id=run_id,
                        response_id=response_id,
                        turn_id=turn_id,
                        sequence=sequence,
                        release_id=release_id,
                        policy_snapshot_id=policy_snapshot_id,
                        occurred_at=terminal_at,
                        cursor=f"{run_id}:{sequence}",
                    ),
                    payload=terminal_payload,
                )
                terminal_event_payload: dict[str, object] = {
                    "kind": terminal_event.kind,
                    "metadata": {
                        "eventId": terminal_event.metadata.event_id,
                        "runId": terminal_event.metadata.run_id,
                        "responseId": terminal_event.metadata.response_id,
                        "turnId": terminal_event.metadata.turn_id,
                        "sequence": terminal_event.metadata.sequence,
                        "cursor": terminal_event.metadata.cursor,
                        "releaseId": terminal_event.metadata.release_id,
                        "policySnapshotId": terminal_event.metadata.policy_snapshot_id,
                        "occurredAt": terminal_event.metadata.occurred_at,
                        "graphId": terminal_event.metadata.graph_id,
                        "nodeId": terminal_event.metadata.node_id,
                        "operationId": terminal_event.metadata.operation_id,
                        "visibility": terminal_event.metadata.visibility,
                    },
                    "payload": dict(terminal_event.payload),
                }
                frozen_terminal_event = _freeze_json_value(
                    "application event stream",
                    "event",
                    terminal_event_payload,
                )
                assert isinstance(frozen_terminal_event, Mapping)
                self._events_by_run_id[run_id] = (*events, frozen_terminal_event)
                self._pending_accepted_runs_by_run_id.pop(run_id, None)
                self._accepted_run_executions_by_run_id.pop(run_id, None)
                completion: dict[str, object] = {
                    "runId": run_id,
                    "status": result_status,
                    "outputs": result_outputs,
                    "lastCursor": f"{run_id}:{sequence}",
                    "duplicate": False,
                }
                frozen_completion = _freeze_json_value(
                    "server accepted run completion",
                    "result",
                    completion,
                )
                assert isinstance(frozen_completion, Mapping)
                self._accepted_run_results_by_run_id[run_id] = frozen_completion
                return completion
        finally:
            with self._accepted_run_condition:
                self._advancing_accepted_runs_by_run_id.pop(run_id, None)
                admission_result = self._accepted_run_results_by_run_id.get(run_id)
                admission_pending = run_id in self._pending_accepted_runs_by_run_id
                self._accepted_run_condition.notify_all()
            promoted_tickets: tuple[AdmissionTicket, ...] = ()
            if (
                admission_ticket_id is not None
                and admission_fencing_token is not None
                and self.admission_ticket_queue is not None
                and admission_result is not None
                and not admission_pending
            ):
                result_status = admission_result.get("status")
                try:
                    if result_status in {"cancelled", "expired"}:
                        _, promoted_tickets = self.admission_ticket_queue.cancel(
                            admission_ticket_id,
                            now_ms=self.admission_clock(),
                            state=(
                                "expired"
                                if result_status == "expired"
                                else "cancelled"
                            ),
                            fencing_token=admission_fencing_token,
                        )
                    else:
                        _, promoted_tickets = self.admission_ticket_queue.complete(
                            admission_ticket_id,
                            admission_fencing_token,
                            "failed" if result_status == "failed" else "completed",
                            now_ms=self.admission_clock(),
                        )
                except AdmissionError:
                    promoted_tickets = ()
            self._dispatch_admitted_tickets(promoted_tickets)

    def callback_submissions(self, operation_id: str) -> tuple[ServerAsyncCallbackSubmission, ...]:
        operation_id = _validate_exact_non_empty_string("server async callback", "operation_id", operation_id)
        return self._callbacks_by_operation_id.get(operation_id, ())

    def async_callback_rejections(self, operation_id: str) -> tuple[dict[str, object], ...]:
        operation_id = _validate_exact_non_empty_string(
            "server async callback rejection",
            "operation_id",
            operation_id,
        )
        return tuple(
            rejection.protocol_value()
            for rejection in self._async_callback_rejections_by_operation_id.get(operation_id, ())
        )

    def late_async_callbacks(self, operation_id: str) -> tuple[dict[str, object], ...]:
        operation_id = _validate_exact_non_empty_string("server late async callback", "operation_id", operation_id)
        return tuple(
            {"kind": "LateExternalCallbackReceived", **rejection.protocol_value()}
            for rejection in self._async_callback_rejections_by_operation_id.get(operation_id, ())
            if rejection.reason == "terminal_run"
        )

    def detachments(self, run_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_exact_non_empty_string("server detach", "run_id", run_id)
        return self._detachments_by_run_id.get(run_id, ())

    def run_controls(self, run_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_exact_non_empty_string("server run control", "run_id", run_id)
        return self._run_controls_by_run_id.get(run_id, ())

    def subscriptions(self, run_id: str) -> tuple[ServerEventSubscription, ...]:
        run_id = _validate_exact_non_empty_string("server event subscription", "run_id", run_id)
        with self._subscription_registration_condition:
            return self._subscriptions_by_run_id.get(run_id, ())

    def event_acks(self, run_id: str, subscription_id: str) -> tuple[dict[str, object], ...]:
        run_id = _validate_exact_non_empty_string("server event ack", "run_id", run_id)
        subscription_id = _validate_exact_non_empty_string(
            "server event ack",
            "subscription_id",
            subscription_id,
        )
        with self._subscription_registration_condition:
            return self._acks_by_subscription.get((run_id, subscription_id), ())

    def callback_registrations(self) -> tuple[ServerCallbackRegistration, ...]:
        with self._callback_registration_condition:
            return tuple(self._callback_registrations[key] for key in sorted(self._callback_registrations))

    def callback_delivery_results(self, subscription_id: str) -> tuple[dict[str, object], ...]:
        subscription_id = _validate_exact_non_empty_string(
            "server callback delivery result",
            "subscription_id",
            subscription_id,
        )
        with self._callback_registration_condition:
            return tuple(
                result.protocol_value()
                for result in self._callback_delivery_results_by_subscription_id.get(subscription_id, ())
            )

    def callback_delivery_redrives(self, delivery_id: str) -> tuple[dict[str, object], ...]:
        delivery_id = _validate_exact_non_empty_string(
            "server callback delivery control",
            "delivery_id",
            delivery_id,
        )
        with self._callback_registration_condition:
            return self._callback_delivery_redrives.get(delivery_id, ())

    def callback_delivery_dead_letter_moves(self, delivery_id: str) -> tuple[dict[str, object], ...]:
        delivery_id = _validate_exact_non_empty_string(
            "server callback delivery control",
            "delivery_id",
            delivery_id,
        )
        with self._callback_registration_condition:
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
        started_at: str | None = None
        updated_at = ""
        completed_at: str | None = None
        state = "running"
        admission_ticket: AdmissionTicket | None = None
        admission_ticket_id = self._admission_ticket_ids_by_run_id.get(run_id)
        if (
            admission_ticket_id is not None
            and self.admission_ticket_queue is not None
        ):
            admission_ticket = self.admission_ticket_queue.get(
                admission_ticket_id
            )
            state = admission_ticket.state
            issued_at = datetime.fromtimestamp(
                admission_ticket.issued_at_ms / 1_000,
                timezone.utc,
            )
            updated_at = (
                issued_at.isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            pending = self._pending_accepted_runs_by_run_id.get(run_id)
            pending_release_id = (
                pending.get("releaseId") if pending is not None else None
            )
            if isinstance(pending_release_id, str):
                release_id = pending_release_id
        terminal_states = {
            "RunSucceeded": "succeeded",
            "RunCompleted": "completed",
            "RunFailed": "failed",
            "RunCancelled": "cancelled",
            "RunPolicyStopped": "policy_stopped",
            "RunExpired": "expired",
        }

        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                raise ValueError("server run status metadata must be an object")
            sequence = metadata.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("server run status sequence must be an integer")
            if sequence < 0:
                raise ValueError("server run status sequence must be non-negative")
            if sequence > last_sequence:
                last_sequence = sequence
            raw_occurred_at = metadata.get("occurredAt")
            if not isinstance(raw_occurred_at, str) or not raw_occurred_at:
                raise ValueError("server run status occurredAt must be an ISO datetime")
            occurred_at = _validate_iso_datetime("server run status", "occurredAt", raw_occurred_at)
            if event.get("kind") == "RunStarted":
                started_at = occurred_at
            updated_at = occurred_at
            event_release_id = metadata.get("releaseId")
            if isinstance(event_release_id, str) and event_release_id:
                release_id = event_release_id
            event_kind = event.get("kind")
            if isinstance(event_kind, str) and event_kind in terminal_states:
                state = terminal_states[event_kind]
                completed_at = updated_at
            elif event_kind == "AsyncOperationWaitingCallback":
                state = "waiting_callback"
            elif event_kind == "RunResuming":
                state = "resuming"

        controls = self._run_controls_by_run_id.get(run_id, ())
        terminal_statuses = {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}
        if controls and state not in terminal_statuses:
            latest_control = controls[-1]
            control_cursor = latest_control.get("lastCursor")
            control_sequence: int | None = None
            if isinstance(control_cursor, str):
                cursor_prefix, separator, cursor_sequence = control_cursor.rpartition(
                    ":"
                )
                if (
                    separator
                    and cursor_prefix == run_id
                    and cursor_sequence.isdigit()
                ):
                    control_sequence = int(cursor_sequence)
            if control_sequence is None or control_sequence >= last_sequence:
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
        current_checkpoint_operation_id: str | None = None
        if admission_ticket is not None and state in {"queued", "admitted"}:
            waiting_on.append(
                {
                    "kind": "admission",
                    "ticketId": admission_ticket.ticket_id,
                    "limiterId": admission_ticket.limiter_id,
                }
            )
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
        execution = self._accepted_run_executions_by_run_id.get(run_id)
        if (
            execution is not None
            and execution.checkpoint is not None
            and state not in terminal_statuses
        ):
            operation = execution.checkpoint.operation
            operation_id = operation.get("operation_id")
            if isinstance(operation_id, str):
                current_checkpoint_operation_id = operation_id
                active_operations.append(operation_id)
                if state == "waiting_callback":
                    waiting = {
                        "kind": "callback",
                        "operationId": operation_id,
                        "nodeId": operation["node_id"],
                        "attemptId": operation["attempt_id"],
                    }
                    waiting_on.append(waiting)
        if state not in {"completed", "succeeded", "failed", "cancelled", "expired", "policy_stopped"}:
            for operation_id in sorted(self._callbacks_by_operation_id):
                submissions = self._callbacks_by_operation_id[operation_id]
                if not submissions:
                    continue
                submission = submissions[-1]
                if submission.run_id != run_id:
                    continue
                if (
                    current_checkpoint_operation_id is not None
                    and submission.operation_id
                    != current_checkpoint_operation_id
                ):
                    continue
                if not active_operations and state in {"running", "waiting_callback"}:
                    waiting: dict[str, object] = {
                        "kind": "callback",
                        "operationId": submission.operation_id,
                    }
                    if submission.node_id is not None:
                        waiting["nodeId"] = submission.node_id
                    if submission.attempt_id is not None:
                        waiting["attemptId"] = submission.attempt_id
                    waiting_on.append(waiting)
                if submission.operation_id not in active_operations:
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
        if admission_ticket is not None:
            payload["admissionTicket"] = admission_ticket.contract()
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
        actor: PrincipalRef | None,
    ) -> ServerResponse:
        occurred_at = _validate_iso_datetime("run control request", "occurred_at", occurred_at)
        if events:
            latest_event_metadata = events[-1].get("metadata", {})
            latest_event_at = (
                latest_event_metadata.get("occurredAt")
                if isinstance(latest_event_metadata, Mapping)
                else None
            )
            latest_event_at = _validate_iso_datetime(
                "run control request",
                "latest_event_at",
                latest_event_at,
            )
            control_datetime = datetime.fromisoformat(
                f"{occurred_at[:-1]}+00:00"
                if occurred_at.endswith("Z")
                else occurred_at
            ).astimezone(timezone.utc)
            latest_event_datetime = datetime.fromisoformat(
                f"{latest_event_at[:-1]}+00:00"
                if latest_event_at.endswith("Z")
                else latest_event_at
            ).astimezone(timezone.utc)
            if control_datetime < latest_event_datetime:
                timestamp_error = (
                    "run control request occurred_at must not be before run start"
                    if len(events) == 1
                    and events[0].get("kind") == "RunStarted"
                    else "run control request occurred_at must not precede latest run event"
                )
                raise ValueError(
                    timestamp_error
                )
        control_states = {
            "cancel_run": "cancelled",
            "pause_run": "paused_operator",
            "resume_run": "resuming",
            "expire_run": "expired",
        }
        pause_states = {
            "operator": "paused_operator",
            "budget": "paused_budget",
            "policy": "paused_policy",
            "callback_delivery": "paused_callback_delivery",
        }
        terminal_control_states = {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
            "policy_stopped",
        }
        event_terminal_state = None
        for event in events:
            event_kind = event.get("kind")
            if event_kind == "RunSucceeded":
                event_terminal_state = "succeeded"
            elif event_kind == "RunCompleted":
                event_terminal_state = "completed"
            elif event_kind == "RunFailed":
                event_terminal_state = "failed"
            elif event_kind == "RunCancelled":
                event_terminal_state = "cancelled"
            elif event_kind == "RunPolicyStopped":
                event_terminal_state = "policy_stopped"
            elif event_kind == "RunExpired":
                event_terminal_state = "expired"
        status = control_states[operation]
        if operation == "pause_run":
            pause_kind = payload.get("pauseKind", "operator")
            if not isinstance(pause_kind, str) or pause_kind not in pause_states:
                return ServerResponse.json(
                    400,
                    {
                        "ok": False,
                        "error": (
                            "run control request pauseKind must be one of operator, budget, policy, or "
                            "callback_delivery"
                        ),
                    },
                )
            status = pause_states[pause_kind]
        reason = payload.get("reason")
        if reason is not None:
            raw_reason = reason
            reason = _validate_non_empty_string("run control request", "reason", raw_reason)
            if raw_reason != reason:
                raise ValueError("run control request reason must not contain surrounding whitespace")
        existing = self._run_controls_by_run_id.get(run_id, ())
        if existing:
            latest_control_at = existing[-1].get("occurredAt")
            latest_control_at = _validate_iso_datetime(
                "run control request",
                "latest_control_at",
                latest_control_at,
            )
            requested_control_datetime = datetime.fromisoformat(
                f"{occurred_at[:-1]}+00:00"
                if occurred_at.endswith("Z")
                else occurred_at
            ).astimezone(timezone.utc)
            latest_control_datetime = datetime.fromisoformat(
                f"{latest_control_at[:-1]}+00:00"
                if latest_control_at.endswith("Z")
                else latest_control_at
            ).astimezone(timezone.utc)
            if requested_control_datetime < latest_control_datetime:
                raise ValueError(
                    "run control request occurred_at must not precede latest run control"
                )
        if existing:
            latest_control = existing[-1]
            current_status = latest_control.get("status")
            if isinstance(current_status, str) and status == current_status:
                existing_reason = latest_control.get("reason")
                if reason != existing_reason:
                    response: dict[str, object] = {
                        "ok": False,
                        "runId": run_id,
                        "status": current_status,
                        "reason": existing_reason,
                        "error": "run control duplicate command conflicts with existing reason",
                    }
                    if reason is not None:
                        response["requestedReason"] = reason
                    return ServerResponse.json(409, response)
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
            if isinstance(current_status, str) and current_status in terminal_control_states:
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_status,
                        "error": f"run {run_id} is terminal with state {current_status}",
                    },
                )
        if event_terminal_state is not None:
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "runId": run_id,
                    "state": event_terminal_state,
                    "error": f"run {run_id} is terminal with state {event_terminal_state}",
                },
            )
        if operation == "resume_run":
            projected_state = self._run_status_payload(
                run_id,
                events,
                include_ok=False,
            ).get("state")
            current_run_state = (
                projected_state
                if isinstance(projected_state, str)
                else "running"
            )
            if current_run_state not in {
                "waiting_input",
                "waiting_approval",
                "waiting_review",
                "waiting_callback",
                "paused_budget",
                "paused_policy",
                "paused_operator",
                "paused_callback_delivery",
            }:
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_run_state,
                        "error": f"run {run_id} is not paused or waiting and cannot be resumed",
                    },
                )
            execution = self._accepted_run_executions_by_run_id.get(run_id)
            if (
                execution is not None
                and execution.checkpoint is not None
                and execution.resume_dispatch_pending
            ):
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_run_state,
                        "error": f"run {run_id} callback resume is already dispatched",
                    },
                )
            if (
                execution is not None
                and execution.checkpoint is not None
                and execution.callback_receipt is None
            ):
                return ServerResponse.json(
                    409,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_run_state,
                        "error": (
                            f"run {run_id} requires a validated callback receipt before resume"
                        ),
                    },
                )
            if (
                execution is not None
                and execution.checkpoint is not None
                and execution.callback_receipt is not None
                and self.accepted_run_executor is None
            ):
                return ServerResponse.json(
                    503,
                    {
                        "ok": False,
                        "runId": run_id,
                        "state": current_run_state,
                        "error": (
                            f"run {run_id} callback resume executor is unavailable"
                        ),
                    },
                )
        pending_run = self._pending_accepted_runs_by_run_id.get(run_id)
        promoted_on_control: tuple[AdmissionTicket, ...] = ()
        if status in {"cancelled", "expired"} and pending_run is not None:
            if events:
                start_metadata = events[0].get("metadata", {})
                started_at = (
                    start_metadata.get("occurredAt")
                    if isinstance(start_metadata, Mapping)
                    else None
                )
                started_at = _validate_iso_datetime(
                    "run control request",
                    "started_at",
                    started_at,
                )
                controlled_datetime = datetime.fromisoformat(
                    f"{occurred_at[:-1]}+00:00"
                    if occurred_at.endswith("Z")
                    else occurred_at
                ).astimezone(timezone.utc)
                started_datetime = datetime.fromisoformat(
                    f"{started_at[:-1]}+00:00"
                    if started_at.endswith("Z")
                    else started_at
                ).astimezone(timezone.utc)
                if controlled_datetime < started_datetime:
                    raise ValueError(
                        "run control request occurred_at must not be before run start"
                    )

            response_id = pending_run.get("responseId")
            release_id = pending_run.get("releaseId")
            policy_snapshot_id = pending_run.get("policySnapshotId")
            turn_id = pending_run.get("turnId")
            if not isinstance(response_id, str):
                raise ValueError("pending accepted run responseId must be a string")
            if not isinstance(release_id, str):
                raise ValueError("pending accepted run releaseId must be a string")
            if not isinstance(policy_snapshot_id, str):
                raise ValueError(
                    "pending accepted run policySnapshotId must be a string"
                )
            if turn_id is not None and not isinstance(turn_id, str):
                raise ValueError("pending accepted run turnId must be a string or null")
            sequence = self._last_event_sequence(events) + 1
            terminal_event = ApplicationEvent.new(
                {
                    "cancelled": "RunCancelled",
                    "expired": "RunExpired",
                }[status],
                ApplicationEventMetadata(
                    event_id=f"{run_id}:run-terminal",
                    run_id=run_id,
                    response_id=response_id,
                    turn_id=turn_id,
                    sequence=sequence,
                    release_id=release_id,
                    policy_snapshot_id=policy_snapshot_id,
                    occurred_at=occurred_at,
                    cursor=f"{run_id}:{sequence}",
                ),
                payload={"status": status, "reason": reason},
            )
            terminal_event_payload: dict[str, object] = {
                "kind": terminal_event.kind,
                "metadata": {
                    "eventId": terminal_event.metadata.event_id,
                    "runId": terminal_event.metadata.run_id,
                    "responseId": terminal_event.metadata.response_id,
                    "turnId": terminal_event.metadata.turn_id,
                    "sequence": terminal_event.metadata.sequence,
                    "cursor": terminal_event.metadata.cursor,
                    "releaseId": terminal_event.metadata.release_id,
                    "policySnapshotId": terminal_event.metadata.policy_snapshot_id,
                    "occurredAt": terminal_event.metadata.occurred_at,
                    "graphId": terminal_event.metadata.graph_id,
                    "nodeId": terminal_event.metadata.node_id,
                    "operationId": terminal_event.metadata.operation_id,
                    "visibility": terminal_event.metadata.visibility,
                },
                "payload": dict(terminal_event.payload),
            }
            frozen_terminal_event = _freeze_json_value(
                "application event stream",
                "event",
                terminal_event_payload,
            )
            assert isinstance(frozen_terminal_event, Mapping)
            events = (*events, frozen_terminal_event)
            self._events_by_run_id[run_id] = events
            self._pending_accepted_runs_by_run_id.pop(run_id, None)
            self._accepted_run_executions_by_run_id.pop(run_id, None)
            active_token = self._advancing_accepted_runs_by_run_id.get(run_id)
            if active_token is not None:
                active_token.cancel(reason or status)
            completion = _freeze_json_value(
                "server accepted run completion",
                "result",
                {
                    "runId": run_id,
                    "status": status,
                    "outputs": {},
                    "lastCursor": f"{run_id}:{sequence}",
                    "duplicate": False,
                },
            )
            assert isinstance(completion, Mapping)
            self._accepted_run_results_by_run_id[run_id] = completion
            ticket_id = self._admission_ticket_ids_by_run_id.get(run_id)
            if (
                ticket_id is not None
                and self.admission_ticket_queue is not None
                and run_id not in self._advancing_accepted_runs_by_run_id
            ):
                _, promoted_on_control = self.admission_ticket_queue.cancel(
                    ticket_id,
                    now_ms=self.admission_clock(),
                    state="expired" if status == "expired" else "cancelled",
                )
            self._accepted_run_condition.notify_all()
        if (
            operation == "resume_run"
            and pending_run is not None
            and self.accepted_run_executor is not None
        ):
            checkpoint_execution = self._accepted_run_executions_by_run_id.get(
                run_id
            )
            resume_submission: ServerAsyncCallbackSubmission | None = None
            if (
                checkpoint_execution is not None
                and checkpoint_execution.checkpoint is not None
                and checkpoint_execution.callback_receipt is not None
            ):
                operation_id = checkpoint_execution.checkpoint.operation.get(
                    "operation_id"
                )
                submissions = (
                    self._callbacks_by_operation_id.get(operation_id, ())
                    if isinstance(operation_id, str)
                    else ()
                )
                resume_submission = next(
                    (
                        submission
                        for submission in reversed(submissions)
                        if submission.run_id == run_id
                    ),
                    None,
                )
                if resume_submission is None:
                    return ServerResponse.json(
                        409,
                        {
                            "ok": False,
                            "runId": run_id,
                            "error": (
                                f"run {run_id} validated callback receipt has no accepted submission"
                            ),
                        },
                    )
                checkpoint_execution.resume_dispatch_pending = True
            try:
                resume_future = self.accepted_run_executor.submit(
                    self.advance_accepted_run,
                    run_id,
                )
                if checkpoint_execution is not None:
                    checkpoint_execution.resume_future = resume_future
                    resume_future.add_done_callback(
                        lambda completed_future, dispatched_run_id=run_id: (
                            self._accepted_run_resume_dispatch_done(
                                dispatched_run_id,
                                completed_future,
                            )
                        )
                    )
            except RuntimeError as error:
                if checkpoint_execution is not None:
                    checkpoint_execution.resume_dispatch_pending = False
                return ServerResponse.json(
                    503,
                    {
                        "ok": False,
                        "runId": run_id,
                        "error": (
                            "accepted run executor rejected resumed work: "
                            f"{error}"
                        ),
                    },
                )
        record_payload: dict[str, object] = {
            "operation": operation,
            "status": status,
            "reason": reason,
            "occurredAt": occurred_at,
            "lastCursor": f"{run_id}:{self._last_event_sequence(events)}",
        }
        if actor is not None:
            record_payload["actor"] = _principal_response_payload(actor)
        record = _freeze_json_value("run control record", "record", record_payload)
        self._run_controls_by_run_id[run_id] = (*existing, record)
        self._accepted_run_condition.notify_all()
        self._dispatch_admitted_tickets(promoted_on_control)
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
        principal: PrincipalRef | None,
    ) -> ServerResponse:
        requested_at = _validate_iso_datetime("callback delivery control request", "requested_at", requested_at)
        delivery_id = _validate_exact_non_empty_string(
            "callback delivery control request",
            "delivery_id",
            delivery_id,
        )
        operator_value = _server_alias_value(
            payload,
            "callback delivery control request",
            "operator",
            "operatorPrincipal",
        )
        if operator_value is None and principal is not None:
            operator = principal.principal_id
        else:
            raw_operator = operator_value if operator_value is not None else ""
            operator = _validate_non_empty_string(
                "callback delivery control request",
                "operator",
                raw_operator,
            )
            if raw_operator != operator:
                raise ValueError("callback delivery control request operator must not contain surrounding whitespace")
        if principal is not None and operator != principal.principal_id:
            raise PermissionError("callback delivery control request operator must match authenticated principal")
        raw_reason = payload.get("reason", "")
        reason = _validate_non_empty_string(
            "callback delivery control request",
            "reason",
            raw_reason,
        )
        if raw_reason != reason:
            raise ValueError("callback delivery control request reason must not contain surrounding whitespace")
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
        with self._callback_registration_condition:
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
        principal: PrincipalRef | None,
    ) -> ServerResponse:
        last_cursor = _server_alias_value(
            payload,
            "attach request",
            "last_cursor",
            "lastCursor",
        )
        if last_cursor is not None:
            last_cursor = _validate_run_cursor("attach request", "last_cursor", run_id, last_cursor)
        capabilities = payload.get("capabilities", ())
        if capabilities is None:
            capabilities = ()
        capabilities_tuple = _validate_string_sequence("attach request", "capabilities", capabilities)
        if any(value != value.strip() for value in capabilities) or any(
            value not in VALID_ATTACH_CAPABILITIES for value in capabilities_tuple
        ):
            raise ValueError("attach request capabilities must contain only supported attach capability literals")

        sequence_by_cursor: dict[str, int] = {}
        last_sequence = 0
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("attach request sequence must be an integer")
            if sequence < 0:
                raise ValueError("attach request sequence must be non-negative")
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
                        "runStatus": self._run_status_payload(run_id, events, include_ok=False),
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
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("attach request sequence must be an integer")
            if sequence < 0:
                raise ValueError("attach request sequence must be non-negative")
            if sequence > replay_after_sequence and _event_visible_to_principal(event, principal):
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
        detached_at = _validate_iso_datetime("detach request", "detached_at", detached_at)
        raw_client_id = _server_alias_value(
            payload,
            "detach request",
            "client_id",
            "clientId",
            "",
        )
        client_id = _validate_non_empty_string(
            "detach request",
            "client_id",
            raw_client_id,
        )
        if raw_client_id != client_id:
            raise ValueError("detach request client_id must not contain surrounding whitespace")
        reason_value = payload.get("reason")
        reason = (
            _validate_non_empty_string("detach request", "reason", reason_value)
            if reason_value is not None
            else None
        )
        if reason_value is not None and reason_value != reason:
            raise ValueError("detach request reason must not contain surrounding whitespace")
        last_sequence = self._last_event_sequence(events, owner="detach request")
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

    def _last_event_sequence(self, events: tuple[dict[str, object], ...], *, owner: str = "server event") -> int:
        last_sequence = 0
        for event in events:
            metadata = event.get("metadata")
            if isinstance(metadata, Mapping):
                sequence = metadata.get("sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    raise ValueError(f"{owner} sequence must be an integer")
                if sequence < 0:
                    raise ValueError(f"{owner} sequence must be non-negative")
                if sequence > last_sequence:
                    last_sequence = sequence
        return last_sequence

    def _append_async_callback_diagnostic_event(
        self,
        kind: str,
        submission: ServerAsyncCallbackSubmission,
        reason: str | None,
        *,
        status: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        if submission.run_id is None:
            return
        events = self._events_by_run_id.get(submission.run_id)
        if events is None:
            return
        event_occurred_at = _validate_iso_datetime(
            "async callback diagnostic event",
            "occurred_at",
            occurred_at or submission.received_at,
        )
        if events:
            latest_event_metadata = events[-1].get("metadata", {})
            latest_event_at = (
                latest_event_metadata.get("occurredAt")
                if isinstance(latest_event_metadata, Mapping)
                else None
            )
            latest_event_at = _validate_iso_datetime(
                "async callback diagnostic event",
                "latest_event_at",
                latest_event_at,
            )
            event_datetime = datetime.fromisoformat(
                f"{event_occurred_at[:-1]}+00:00"
                if event_occurred_at.endswith("Z")
                else event_occurred_at
            ).astimezone(timezone.utc)
            latest_event_datetime = datetime.fromisoformat(
                f"{latest_event_at[:-1]}+00:00"
                if latest_event_at.endswith("Z")
                else latest_event_at
            ).astimezone(timezone.utc)
            if event_datetime < latest_event_datetime:
                event_occurred_at = latest_event_at
        sequence = self._last_event_sequence(events, owner="async callback diagnostic event") + 1
        run_status = self._run_status_payload(submission.run_id, events, include_ok=False)
        release_id = run_status.get("releaseId")
        if not isinstance(release_id, str) or not release_id:
            release_id = "local"
        payload: dict[str, object] = {
            "callbackId": submission.callback_id,
            "idempotencyKey": submission.idempotency_key,
            "payloadDigest": submission.payload_digest,
            "verifiedBy": submission.verified_by,
            "policySnapshotId": submission.policy_snapshot_id,
            "receivedAt": submission.received_at,
        }
        if reason is not None:
            payload["reason"] = reason
        if submission.attempt_id is not None:
            payload["attemptId"] = submission.attempt_id
        if submission.provider_operation_id is not None:
            payload["providerOperationId"] = submission.provider_operation_id
        if status is not None:
            payload["status"] = status
        event = ApplicationEvent.new(
            kind,
            ApplicationEventMetadata(
                event_id=f"{submission.run_id}:callback-diagnostic:{sequence}",
                run_id=submission.run_id,
                response_id=f"callback:{submission.callback_id}",
                sequence=sequence,
                release_id=release_id,
                policy_snapshot_id=submission.policy_snapshot_id,
                occurred_at=event_occurred_at,
                cursor=f"{submission.run_id}:{sequence}",
                node_id=submission.node_id,
                operation_id=submission.operation_id,
                visibility="operator",
            ),
            payload=payload,
        )
        event_payload = {
            "kind": event.kind,
            "metadata": {
                "eventId": event.metadata.event_id,
                "runId": event.metadata.run_id,
                "responseId": event.metadata.response_id,
                "turnId": event.metadata.turn_id,
                "sequence": event.metadata.sequence,
                "cursor": event.metadata.cursor,
                "releaseId": event.metadata.release_id,
                "policySnapshotId": event.metadata.policy_snapshot_id,
                "occurredAt": event.metadata.occurred_at,
                "graphId": event.metadata.graph_id,
                "nodeId": event.metadata.node_id,
                "operationId": event.metadata.operation_id,
                "visibility": event.metadata.visibility,
            },
            "payload": dict(event.payload),
        }
        with self._accepted_run_condition:
            current_events = self._events_by_run_id.get(submission.run_id)
            if current_events is None:
                return
            current_sequence = (
                self._last_event_sequence(
                    current_events,
                    owner="async callback diagnostic event",
                )
                + 1
            )
            if current_sequence != sequence:
                metadata_payload = event_payload["metadata"]
                assert isinstance(metadata_payload, dict)
                metadata_payload["eventId"] = (
                    f"{submission.run_id}:callback-diagnostic:{current_sequence}"
                )
                metadata_payload["sequence"] = current_sequence
                metadata_payload["cursor"] = (
                    f"{submission.run_id}:{current_sequence}"
                )
            self._events_by_run_id[submission.run_id] = (
                *current_events,
                _freeze_json_value(
                    "application event stream",
                    "event",
                    event_payload,
                ),
            )

    def _subscription_replay(
        self,
        subscription: ServerEventSubscription,
        events: tuple[dict[str, object], ...],
    ) -> list[dict[str, object]] | ServerResponse:
        replay_after_sequence = 0
        sequence_by_cursor: dict[str, int] = {}
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            sequence = metadata.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("server event subscription sequence must be an integer")
            if sequence < 0:
                raise ValueError("server event subscription sequence must be non-negative")
            sequence_by_cursor[f"{subscription.run_id}:{sequence}"] = sequence
        if subscription.replay_from_cursor is not None:
            _validate_run_cursor(
                "server event subscription",
                "replay_from_cursor",
                subscription.run_id,
                subscription.replay_from_cursor,
            )
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
                        "runStatus": self._run_status_payload(subscription.run_id, events, include_ok=False),
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
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("server event subscription sequence must be an integer")
            if sequence < 0:
                raise ValueError("server event subscription sequence must be non-negative")
            if sequence <= replay_after_sequence:
                continue
            if (
                _event_visible_to_principal(event, subscription.owner)
                and self._event_matches_subscription_filter(event, subscription.event_filter)
            ):
                replayed_events.append(_response_json_object(event))
        return replayed_events

    def _event_matches_subscription_filter(self, event: Mapping[str, object], event_filter: Mapping[str, object]) -> bool:
        event_kind = event.get("kind")
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        event_sources = (event, metadata, payload)
        visibility_filter = event_filter.get("visibility")
        if visibility_filter is not None:
            allowed_visibility = _validate_string_sequence(
                "server event subscription",
                "event_filter.visibility",
                visibility_filter,
            )
            visibility_matches = False
            for source in event_sources:
                value = source.get("visibility")
                if isinstance(value, str) and value in allowed_visibility:
                    visibility_matches = True
            if "client" in allowed_visibility and not any(
                isinstance(source.get("visibility"), str) for source in event_sources
            ):
                visibility_matches = True
            if not visibility_matches:
                return False
        node_filter = event_filter.get("node_ids", event_filter.get("nodeIds"))
        if node_filter is not None:
            allowed_nodes = _validate_string_sequence("server event subscription", "event_filter.node_ids", node_filter)
            node_matches = False
            for source in event_sources:
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
            for source in event_sources:
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
        if (
            isinstance(event_kind, str)
            and event_kind in SERVER_TERMINAL_EVENT_KINDS
            and not include_terminal_events
        ):
            return False
        types = event_filter.get("types")
        if types is None:
            return True
        allowed_types = _validate_string_sequence("server event subscription", "event_filter.types", types)
        return isinstance(event_kind, str) and event_kind in allowed_types

    def _subscription_for(self, run_id: str, subscription_id: str) -> ServerEventSubscription | None:
        with self._subscription_registration_condition:
            for subscription in self._subscriptions_by_run_id.get(run_id, ()):
                if subscription.subscription_id == subscription_id:
                    return subscription
        return None

    def _ack_event_response(
        self,
        run_id: str,
        subscription_id: str,
        subscription: ServerEventSubscription,
        events: tuple[dict[str, object], ...],
        payload: Mapping[str, object],
        acknowledged_at: str,
    ) -> ServerResponse:
        acknowledged_at = _validate_iso_datetime("ack request", "acknowledged_at", acknowledged_at)
        event_id = _server_alias_value(
            payload,
            "ack request",
            "event_id",
            "eventId",
        )
        cursor = payload.get("cursor")
        if event_id is None and cursor is None:
            raise ValueError("ack request requires event_id or cursor")
        event_id_text = (
            _validate_non_empty_string("ack request", "event_id", event_id)
            if event_id is not None
            else None
        )
        cursor_text = (
            _validate_run_cursor("ack request", "cursor", run_id, cursor)
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
        matched_sequence = metadata.get("sequence")
        if not isinstance(matched_sequence, int) or isinstance(matched_sequence, bool):
            raise ValueError("ack request sequence must be an integer")
        if matched_sequence < 0:
            raise ValueError("ack request sequence must be non-negative")
        matched_cursor = f"{run_id}:{matched_sequence}"
        matched_event_id = metadata.get("eventId")
        if (
            event_id_text is not None
            and cursor_text is not None
            and (matched_event_id != event_id_text or matched_cursor != cursor_text)
        ):
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "runId": run_id,
                    "subscriptionId": subscription_id,
                    "eventId": event_id_text,
                    "cursor": cursor_text,
                    "error": "ack event_id and cursor refer to different retained events",
                },
            )
        if not _event_visible_to_principal(matched_event, subscription.owner):
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "runId": run_id,
                    "subscriptionId": subscription_id,
                    "eventId": str(matched_event_id) if isinstance(matched_event_id, str) else event_id_text,
                    "cursor": matched_cursor,
                    "error": "acknowledged event is not visible to the subscription principal",
                },
            )
        if not self._event_matches_subscription_filter(matched_event, subscription.event_filter):
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "runId": run_id,
                    "subscriptionId": subscription_id,
                    "eventId": str(matched_event_id) if isinstance(matched_event_id, str) else event_id_text,
                    "cursor": matched_cursor if matched_cursor is not None else cursor_text,
                    "error": "acknowledged event is not selected by the subscription filter",
                },
            )
        event_id_text = str(metadata.get("eventId", event_id_text or ""))
        cursor_text = matched_cursor if matched_cursor is not None else cursor_text
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
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError("ack request sequence must be an integer")
            if sequence < 0:
                raise ValueError("ack request sequence must be non-negative")
            event_cursor = f"{run_id}:{sequence}"
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
            owner=registration.owner,
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
    "APPLICATION_COMMAND_KINDS",
    "APPLICATION_PROTOCOL_EVENT_KINDS",
    "ApplicationCommand",
    "ApplicationCommandKind",
    "ApplicationCommandMetadata",
    "ApplicationEvent",
    "ApplicationEventMetadata",
    "ApplicationProtocolCapabilities",
    "ApplicationProtocolError",
    "ApplicationProtocolEvent",
    "ApplicationProtocolEventKind",
    "ApplicationProtocolEventMetadata",
    "ApplicationProtocolLog",
    "GraphBlocksServerApp",
    "ServerAuthDecision",
    "ServerAsyncCallbackRejection",
    "ServerAsyncCallbackResumeAdmissionHook",
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
