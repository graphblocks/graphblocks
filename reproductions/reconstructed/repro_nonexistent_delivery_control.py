"""Reconstructed GB-SEC-007 delivery-control harness from captured output."""

from __future__ import annotations

import json

from graphblocks.policy import PrincipalRef
from graphblocks.server import GraphBlocksServerApp, ServerRequest, StaticBearerAuthHook


def main() -> int:
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token": PrincipalRef("operator")})
    )
    for operation in ("redrive", "dead-letter"):
        response = app.handle(
            ServerRequest(
                method="POST",
                path=f"/callbacks/deliveries/never-existed/{operation}",
                headers={"Authorization": "Bearer token"},
                query={},
                cookies={},
                body=json.dumps({"reason": "audit reproduction"}).encode("utf-8"),
                requested_at="2026-07-27T00:00:00Z",
            )
        )
        if response.status_code != 404:
            raise SystemExit(f"unknown delivery {operation} returned {response.status_code}")
    if app.callback_delivery_redrives("never-existed") or app.callback_delivery_dead_letter_moves(
        "never-existed"
    ):
        raise SystemExit("unknown delivery control mutated server state")
    print("GB-SEC-007 fixed: unknown delivery controls return 404 without mutation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
