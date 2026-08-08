from __future__ import annotations

from graphblocks.policy import PrincipalRef
from graphblocks.server import (
    ServerAuthRequest,
    ServerHealth,
    ServerRequest,
    ServerRequestHead,
    ServerResponse,
    ServerRouteMatch,
    StaticBearerAuthHook,
)


def mutate_frozen_server_mappings(
    route_match: ServerRouteMatch,
    auth_request: ServerAuthRequest,
    request_head: ServerRequestHead,
    request: ServerRequest,
    response: ServerResponse,
    auth_hook: StaticBearerAuthHook,
    health: ServerHealth,
) -> None:
    route_match.path_params["run_id"] = "changed"
    auth_request.headers["authorization"] = "changed"
    auth_request.query["cursor"] = "changed"
    auth_request.cookies["session"] = "changed"
    request_head.headers["content-length"] = "0"
    request_head.query["cursor"] = "changed"
    request_head.cookies["session"] = "changed"
    request.headers["content-type"] = "text/plain"
    request.query["cursor"] = "changed"
    request.cookies["session"] = "changed"
    response.headers["content-type"] = "text/plain"
    auth_hook.principals_by_token["new-token"] = PrincipalRef("new-user")
    health.checks[0][2]["detail"] = "changed"
