from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from typing import Literal, Protocol

from .application_event import ApplicationEvent, ApplicationEventMetadata
from .canonical import canonical_hash
from .policy import PrincipalRef
from .runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry


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
class ServerRouteMatch:
    endpoint: ServerEndpoint
    path_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_params", dict(self.path_params))


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

    def match(self, method: str, path: str) -> ServerRouteMatch:
        normalized_method = method.upper()
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
class ServerRequest:
    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    cookies: dict[str, str]
    body: bytes = b""
    requested_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "headers", {key.lower(): value for key, value in self.headers.items()})
        object.__setattr__(self, "query", dict(self.query))
        object.__setattr__(self, "cookies", dict(self.cookies))
        object.__setattr__(self, "body", bytes(self.body))


@dataclass(frozen=True, slots=True)
class ServerResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def read(self) -> bytes:
        return self.body

    @classmethod
    def json(cls, status_code: int, payload: dict[str, object]) -> ServerResponse:
        return cls(
            status_code=status_code,
            headers={"content-type": "application/json"},
            body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )


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


@dataclass(slots=True)
class GraphBlocksServerApp:
    route_manifest: ServerRouteManifest = field(default_factory=default_server_route_manifest)
    auth_hook: ServerAuthHook | None = None
    health: ServerHealth = field(default_factory=lambda: ServerHealth("graphblocks-api"))
    registry: RuntimeRegistry = field(default_factory=stdlib_registry)

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
        if route.operation == "cancel_run":
            return ServerResponse.json(
                202,
                {
                    "ok": True,
                    "runId": route_match.path_params.get("run_id", ""),
                    "status": "cancel_requested",
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
                run_id = str(payload.get("runId", payload.get("run_id", "run-000001")))
                response_id = str(payload.get("responseId", payload.get("response_id", "response-000001")))
                release_id = str(payload.get("releaseId", payload.get("release_id", "local")))
                policy_snapshot_id = str(
                    payload.get("policySnapshotId", payload.get("policy_snapshot_id", "local"))
                )
                occurred_at = str(payload.get("occurredAt", payload.get("occurred_at", "")))
                turn_id_value = payload.get("turnId", payload.get("turn_id"))
                turn_id = str(turn_id_value) if turn_id_value is not None else None

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
    "GraphBlocksServerApp",
    "ServerAuthDecision",
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
