from __future__ import annotations

import ast
from io import BytesIO
import json
from pathlib import Path

import graphblocks.server as graphblocks_server
import pytest

from graphblocks.admission import AdmissionTicketQueue
from graphblocks.server import (
    GraphBlocksServerApp,
    ServerErrorAuditEvent,
    ServerRequest,
    ServerRequestHead,
)


ROOT = Path(__file__).parents[1]
SECRET = "secret=provider-token internal=/srv/private/compiler.py"


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception string unavailable")


def _error_audit_event(**overrides: object) -> ServerErrorAuditEvent:
    values = {
        "method": "POST",
        "route": "/runs",
        "operation": "invoke_graph",
        "status_code": 500,
        "error_code": "server.operation.failed",
        "correlation_id": "error-correlation",
        "failure_type": "builtins.RuntimeError",
        "failure_detail": "internal failure",
        "observed_at": "2026-08-08T00:00:00Z",
    }
    values.update(overrides)
    return ServerErrorAuditEvent(**values)  # type: ignore[arg-type]


def _run_request(run_id: str) -> ServerRequest:
    return ServerRequest(
        method="POST",
        path="/runs",
        headers={},
        query={},
        cookies={},
        body=json.dumps(
            {
                "graph": {
                    "apiVersion": "graphblocks.ai/v1alpha3",
                    "kind": "Graph",
                    "metadata": {"name": "safe-server-errors"},
                    "spec": {"nodes": {}},
                },
                "runId": run_id,
                "responseMode": "background",
                "occurredAt": "2026-08-08T00:00:00Z",
            }
        ).encode("utf-8"),
    )


def test_internal_exception_is_correlated_without_public_detail_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []

    def fail_compile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ValueError(SECRET)

    monkeypatch.setattr(graphblocks_server, "compile_graph", fail_compile)
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        error_correlation_id_factory=lambda: "error-correlation-1",
        error_audit_hook=observed.append,
    )

    response = app.handle(_run_request("run-safe-error-1"))

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "ok": False,
        "errorCode": "server.run.compilation_failed",
        "message": "The run could not be compiled.",
        "correlationId": "error-correlation-1",
    }
    assert response.headers["x-correlation-id"] == "error-correlation-1"
    assert SECRET.encode("utf-8") not in response.body

    assert observed == list(app.error_audit_events())
    assert len(observed) == 1
    event = observed[0]
    assert event.method == "POST"
    assert event.route == "/runs"
    assert event.operation == "invoke_graph"
    assert event.status_code == 500
    assert event.error_code == "server.run.compilation_failed"
    assert event.correlation_id == "error-correlation-1"
    assert event.failure_type == "builtins.ValueError"
    assert event.failure_detail == SECRET


def test_error_audit_is_bounded_and_hook_failure_cannot_change_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correlations = iter(("error-correlation-1", "error-correlation-2"))

    def fail_compile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(SECRET)

    def fail_audit_hook(event: object) -> None:
        del event
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(graphblocks_server, "compile_graph", fail_compile)
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        max_error_audit_events=1,
        error_correlation_id_factory=lambda: next(correlations),
        error_audit_hook=fail_audit_hook,
    )

    first = app.handle(_run_request("run-safe-error-1"))
    second = app.handle(_run_request("run-safe-error-2"))

    assert first.status_code == second.status_code == 500
    assert json.loads(second.body)["correlationId"] == "error-correlation-2"
    retained = app.error_audit_events()
    assert len(retained) == 1
    assert retained[0].correlation_id == "error-correlation-2"
    assert retained[0].failure_detail == SECRET


def test_invalid_correlation_factory_falls_back_to_server_generated_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graphblocks_server,
        "compile_graph",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError(SECRET)),
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        error_correlation_id_factory=lambda: " ",
    )

    response = app.handle(_run_request("run-safe-error-fallback"))
    payload = json.loads(response.body)

    assert response.status_code == 500
    assert payload["correlationId"]
    assert payload["correlationId"] != " "
    assert response.headers["x-correlation-id"] == payload["correlationId"]


def test_error_audit_contract_rejects_invalid_status_and_oversized_fields() -> None:
    with pytest.raises(ValueError, match="HTTP error status"):
        _error_audit_event(status_code=399)
    with pytest.raises(ValueError, match="error_code exceeds byte limit"):
        _error_audit_event(error_code="e" * 257)


def test_server_rejects_invalid_error_audit_dependencies() -> None:
    with pytest.raises(ValueError, match="error_audit_hook must be callable"):
        GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            error_audit_hook=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(
        ValueError,
        match="error_correlation_id_factory must be callable",
    ):
        GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            error_correlation_id_factory=object(),  # type: ignore[arg-type]
        )


def test_error_audit_hashes_oversized_or_noncanonical_internal_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_error = type("Sensitive" * 40, (RuntimeError,), {})

    def fail_compile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise oversized_error(" ")

    monkeypatch.setattr(graphblocks_server, "compile_graph", fail_compile)
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        error_correlation_id_factory=lambda: "c" * 257,
    )

    response = app.handle(_run_request("run-safe-error-hashed"))
    event = app.error_audit_events()[0]

    assert response.status_code == 500
    assert json.loads(response.body)["correlationId"] != "c" * 257
    assert event.failure_type.startswith("sha256:")
    assert event.failure_detail.startswith("sha256:")


def test_route_lookup_failures_use_the_safe_error_contract() -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)
    request = ServerRequest(
        method="GET",
        path="/missing",
        headers={},
        query={},
        cookies={},
    )
    request_head = ServerRequestHead(
        method="GET",
        path="/missing",
        headers={},
        query={},
        cookies={},
    )

    direct = app.handle(request)
    streamed = app.handle_stream(
        request_head,
        BytesIO(b"").read,
        abort_body=lambda: None,
    )

    for response in (direct, streamed):
        assert response.status_code == 404
        assert json.loads(response.body)["errorCode"] == "server.route.not_found"


def test_unexpected_operation_failure_uses_the_safe_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_operation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(SECRET)

    monkeypatch.setattr(
        graphblocks_server,
        "_SERVER_OPERATION_HANDLERS",
        {"system": fail_operation},
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        error_correlation_id_factory=lambda: "error-correlation-operation",
    )

    response = app.handle(
        ServerRequest(
            method="GET",
            path="/health",
            headers={},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "ok": False,
        "errorCode": "server.operation.failed",
        "message": "The server could not complete the request.",
        "correlationId": "error-correlation-operation",
    }
    assert app.error_audit_events()[0].failure_detail == SECRET


def test_admitted_run_persistence_failure_uses_the_safe_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_persistence(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(SECRET)

    monkeypatch.setattr(
        GraphBlocksServerApp,
        "_record_run_authorization",
        fail_persistence,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=AdmissionTicketQueue(
            "safe-errors",
            max_concurrent=1,
            rate_limit=1,
            window_ms=1_000,
            max_pending=1,
            ticket_ttl_ms=60_000,
        ),
        admission_clock=lambda: 0,
        error_correlation_id_factory=lambda: "error-correlation-persistence",
    )

    response = app.handle(_run_request("run-safe-error-persistence"))

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "ok": False,
        "errorCode": "server.run.persistence_failed",
        "message": "The run could not be persisted.",
        "correlationId": "error-correlation-persistence",
    }
    assert app.error_audit_events()[0].failure_detail == SECRET


def test_unprintable_exception_cannot_break_safe_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_compile(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise _UnprintableError

    monkeypatch.setattr(graphblocks_server, "compile_graph", fail_compile)
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        error_correlation_id_factory=lambda: "error-correlation-unprintable",
    )

    response = app.handle(_run_request("run-safe-error-unprintable"))

    assert response.status_code == 500
    assert json.loads(response.body)["errorCode"] == "server.run.compilation_failed"
    assert app.error_audit_events()[0].failure_detail == "unprintable-exception"


def test_server_responses_never_render_caught_exception_variables() -> None:
    source = (ROOT / "src" / "graphblocks" / "server.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    leaks: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "json"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ServerResponse"
        ):
            continue
        names = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
        }
        if names & {"error", "route_error"}:
            leaks.append(node.lineno)

    assert leaks == []
