from __future__ import annotations
import json
from graphblocks import GraphBlocksServerApp, PrincipalRef, ServerRequest, StaticBearerAuthHook

def call(app, method, path, token, body=None):
    response = app.handle(ServerRequest(
        method=method,
        path=path,
        headers={"Authorization": f"Bearer {token}"},
        query={}, cookies={},
        body=b"" if body is None else json.dumps(body).encode(),
        requested_at="2026-07-27T00:00:01Z",
    ))
    return response.status_code, json.loads(response.body)

app = GraphBlocksServerApp(
    auth_hook=StaticBearerAuthHook({
        "alice-token": PrincipalRef("alice", tenant_id="tenant-a"),
        "bob-token": PrincipalRef("bob", tenant_id="tenant-b"),
    }),
    defer_accepted_runs=True,
)
graph = {
    "apiVersion": "graphblocks.ai/v1alpha3", "kind": "Graph",
    "metadata": {"name": "control-repro"}, "spec": {"nodes": {}},
}
print("alice-create", call(app, "POST", "/runs", "alice-token", {
    "graph": graph, "runId": "alice-pending", "responseMode": "accepted",
    "occurredAt": "2026-07-27T00:00:00Z",
}))
print("bob-cancel", call(app, "POST", "/runs/alice-pending/cancel", "bob-token", {"reason": "cross-tenant"}))
print("alice-status", call(app, "GET", "/runs/alice-pending", "alice-token"))
