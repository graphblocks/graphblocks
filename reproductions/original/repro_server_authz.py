from __future__ import annotations
import json
from graphblocks import GraphBlocksServerApp, PrincipalRef, ServerRequest, StaticBearerAuthHook


def request(app: GraphBlocksServerApp, method: str, path: str, token: str | None = None, body: object | None = None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    response = app.handle(ServerRequest(method=method, path=path, headers=headers, query={}, cookies={}, body=raw))
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except Exception:
        payload = response.body.decode("utf-8", errors="replace")
    return response.status_code, payload


graph = {
    "apiVersion": "graphblocks.ai/v1alpha3",
    "kind": "Graph",
    "metadata": {"name": "authz-repro"},
    "spec": {
        "nodes": {
            "render": {
                "block": "prompt.render@1",
                "config": {"template": "secret={message.text}"},
                "inputs": {"message": "$input.message"},
                "outputs": {"prompt": "$output.prompt"},
            }
        }
    },
}

# Repro 1: protected route is open when auth_hook is omitted.
open_app = GraphBlocksServerApp()
print("default-no-auth-hook", request(open_app, "GET", "/runs"))

# Repro 2: valid Bob token can read Alice's run across tenant boundary.
app = GraphBlocksServerApp(auth_hook=StaticBearerAuthHook({
    "alice-token": PrincipalRef("alice", tenant_id="tenant-a"),
    "bob-token": PrincipalRef("bob", tenant_id="tenant-b"),
}))
print("alice-create", request(app, "POST", "/runs", "alice-token", {
    "graph": graph,
    "inputs": {"message": {"text": "alice-only"}},
    "runId": "run-alice",
    "responseId": "response-alice",
    "releaseId": "release-a",
    "policySnapshotId": "policy-a",
    "occurredAt": "2026-07-27T00:00:00Z",
}))
print("bob-list", request(app, "GET", "/runs", "bob-token"))
print("bob-status", request(app, "GET", "/runs/run-alice", "bob-token"))
print("bob-attach", request(app, "POST", "/runs/run-alice/attach", "bob-token", {
    "lastCursor": "run-alice:0",
}))
