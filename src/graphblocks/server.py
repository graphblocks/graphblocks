from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from .canonical import canonical_hash
from .policy import PrincipalRef


ServerTransport = Literal["http", "sse", "websocket"]
ServerHealthStatus = Literal["healthy", "degraded", "unhealthy"]


@dataclass(frozen=True, slots=True)
class ServerEndpoint:
    method: str
    path: str
    transport: ServerTransport
    operation: str
    auth_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())

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
class ServerRouteManifest:
    endpoints: tuple[ServerEndpoint, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoints", tuple(self.endpoints))

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
        return tuple(endpoint for endpoint in self.endpoints if endpoint.transport == transport)

    def lookup(self, method: str, path: str) -> ServerEndpoint:
        normalized_method = method.upper()
        for endpoint in self.endpoints:
            if endpoint.method == normalized_method and endpoint.path == path:
                return endpoint
        raise ServerRouteNotFoundError(method, path)

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
            ServerEndpoint("POST", "/runs", "http", "invoke_graph", auth_required=True),
            ServerEndpoint("POST", "/runs/{run_id}/cancel", "http", "cancel_run", auth_required=True),
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
        object.__setattr__(self, "headers", {key.lower(): value for key, value in self.headers.items()})
        object.__setattr__(self, "query", dict(self.query))
        object.__setattr__(self, "cookies", dict(self.cookies))


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
        object.__setattr__(self, "principals_by_token", dict(self.principals_by_token))

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
        object.__setattr__(
            self,
            "checks",
            tuple((name, status, dict(details)) for name, status, details in self.checks),
        )

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
                    "details": details,
                }
                for name, status, details in self.checks
            },
        }


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
        object.__setattr__(self, "commands", tuple(sorted(set(self.commands))))
        object.__setattr__(self, "events", tuple(sorted(set(self.events))))

    def with_commands(self, commands: list[str] | tuple[str, ...]) -> ApplicationProtocolCapabilities:
        return replace(self, commands=tuple(commands))

    def with_events(self, events: list[str] | tuple[str, ...]) -> ApplicationProtocolCapabilities:
        return replace(self, events=tuple(events))

    def negotiate(self, peer: ApplicationProtocolCapabilities) -> ApplicationProtocolCapabilities:
        if self.protocol_version != peer.protocol_version:
            raise ServerProtocolVersionMismatchError(self.protocol_version, peer.protocol_version)
        return ApplicationProtocolCapabilities(
            protocol_version=self.protocol_version,
            commands=tuple(sorted(set(self.commands).intersection(peer.commands))),
            events=tuple(sorted(set(self.events).intersection(peer.events))),
        )


__all__ = [
    "ApplicationProtocolCapabilities",
    "ServerAuthDecision",
    "ServerAuthHook",
    "ServerAuthRequest",
    "ServerEndpoint",
    "ServerHealth",
    "ServerHealthStatus",
    "ServerProtocolVersionMismatchError",
    "ServerRouteManifest",
    "ServerRouteNotFoundError",
    "ServerTransport",
    "StaticBearerAuthHook",
    "default_server_route_manifest",
]
