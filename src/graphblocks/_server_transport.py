"""Server transport contracts.

Extracted verbatim from ``graphblocks.server`` (which re-imports every name,
so the ``graphblocks.server`` facade is unchanged): endpoints, the route
manifest, the request/response envelopes, and the bounded streaming body
reader with its error types.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import json
from types import MappingProxyType
from urllib.parse import unquote

from ._canonical_reference import canonical_dumps, canonical_hash
from ._server_validation import (
    _freeze_json_value,
    _response_json_object,
    _SERVER_REQUEST_BODY_READ_CHUNK_BYTES,
    _validate_http_headers,
    _validate_http_message_framing,
    _validate_http_message_head_framing,
    _validate_non_empty_string,
    _validate_route_path,
    _validate_string_mapping,
    _validate_transport,
    ServerTransport,
)

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
    path_params: Mapping[str, str] = field(default_factory=dict)

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
            ServerEndpoint("DELETE", "/runs/{run_id}", "http", "delete_run", auth_required=True),
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
    headers: Mapping[str, str]
    query: Mapping[str, str]
    cookies: Mapping[str, str]
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
class ServerRequestHead:
    """Normalized HTTP request metadata without an already-buffered body."""

    method: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    cookies: Mapping[str, str]
    requested_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method",
            _validate_non_empty_string(
                "server request head",
                "method",
                self.method,
            ).upper(),
        )
        object.__setattr__(
            self,
            "path",
            _validate_route_path("server request head", self.path),
        )
        object.__setattr__(
            self,
            "headers",
            _validate_http_headers("server request head", self.headers),
        )
        object.__setattr__(
            self,
            "query",
            _validate_string_mapping(
                "server request head",
                "query",
                self.query,
            ),
        )
        object.__setattr__(
            self,
            "cookies",
            _validate_string_mapping(
                "server request head",
                "cookies",
                self.cookies,
            ),
        )
        object.__setattr__(
            self,
            "requested_at",
            (
                ""
                if self.requested_at == ""
                else _validate_non_empty_string(
                    "server request head",
                    "requested_at",
                    self.requested_at,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ServerRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    cookies: Mapping[str, str]
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
    headers: Mapping[str, str]
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
    def json(
        cls,
        status_code: int,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> ServerResponse:
        if not isinstance(payload, Mapping):
            raise ValueError("server response JSON payload must be a mapping")
        payload_copy = _response_json_object(_freeze_json_value("server response JSON", "payload", payload))
        canonical_dumps(payload_copy)
        response_headers = {"content-type": "application/json"}
        if headers is not None:
            additional_headers = _validate_http_headers(
                "server response JSON",
                headers,
            )
            if "content-type" in additional_headers:
                raise ValueError(
                    "server response JSON headers must not override content-type"
                )
            response_headers.update(additional_headers)
        return cls(
            status_code=status_code,
            headers=response_headers,
            body=json.dumps(payload_copy, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8"),
        )


class _ServerRequestBodyTooLargeError(ValueError):
    def __init__(
        self,
        body_size_bytes: int,
        max_body_bytes: int,
        *,
        exact_size: bool,
    ) -> None:
        super().__init__("server request body exceeds max body bytes")
        self.body_size_bytes = body_size_bytes
        self.max_body_bytes = max_body_bytes
        self.exact_size = exact_size


class _ServerRequestBodyLengthMismatchError(ValueError):
    pass


def _read_bounded_server_request_body(
    request: ServerRequestHead,
    read_body: Callable[[int], bytes],
    *,
    max_body_bytes: int,
) -> bytes:
    if not isinstance(request, ServerRequestHead):
        raise TypeError("streaming server request must be a ServerRequestHead")
    if not callable(read_body):
        raise TypeError("streaming server request body reader must be callable")
    if (
        isinstance(max_body_bytes, bool)
        or not isinstance(max_body_bytes, int)
        or max_body_bytes < 1
    ):
        raise ValueError(
            "streaming server request max_body_bytes must be a positive integer"
        )

    content_length = _validate_http_message_head_framing(
        "server request",
        request.headers,
    )
    if content_length is not None and content_length > max_body_bytes:
        raise _ServerRequestBodyTooLargeError(
            content_length,
            max_body_bytes,
            exact_size=True,
        )

    body = bytearray()
    target_bytes = max_body_bytes + 1
    while len(body) < target_bytes:
        read_size = min(
            _SERVER_REQUEST_BODY_READ_CHUNK_BYTES,
            target_bytes - len(body),
        )
        try:
            chunk = read_body(read_size)
        except TypeError:
            raise ValueError(
                "streaming server request body reader must accept a size limit"
            ) from None
        if type(chunk) is not bytes:
            raise ValueError(
                "streaming server request body reader must return exact bytes"
            )
        if len(chunk) > read_size:
            raise ValueError(
                "streaming server request body reader returned more than "
                f"{read_size} bytes"
            )
        if not chunk:
            break
        body.extend(chunk)

    if len(body) > max_body_bytes:
        raise _ServerRequestBodyTooLargeError(
            len(body),
            max_body_bytes,
            exact_size=False,
        )
    if content_length is not None and content_length != len(body):
        raise _ServerRequestBodyLengthMismatchError(
            "server request content-length must match body length"
        )
    return bytes(body)
