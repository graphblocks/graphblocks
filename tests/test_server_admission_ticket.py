from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Lock

import pytest

from graphblocks.admission import AdmissionTicketQueue
from graphblocks.runtime import RuntimeRegistry
from graphblocks.server import GraphBlocksServerApp, ServerRequest


def _graph(block: str = "prompt.render@1") -> dict[str, object]:
    node: dict[str, object] = {
        "block": block,
        "outputs": {"value": "$output.value"},
    }
    if block == "prompt.render@1":
        node = {
            "block": block,
            "config": {"template": "Ticketed {message.text}"},
            "inputs": {"message": "$input.message"},
            "outputs": {"prompt": "$output.prompt"},
        }
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "ticketed-admission"},
        "spec": {"nodes": {"work": node}},
    }


def _submit(
    app: GraphBlocksServerApp,
    run_id: str,
    *,
    graph: dict[str, object] | None = None,
) -> dict[str, object]:
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph or _graph(),
                    "inputs": {"message": {"text": run_id}},
                    "runId": run_id,
                    "requestId": f"request-{run_id}",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-10T00:00:00Z",
                }
            ).encode(),
        )
    )
    assert response.status_code == 202
    return json.loads(response.body)


def test_ticketed_server_returns_cursor_zero_and_promotes_fifo() -> None:
    clock = [0]
    queue = AdmissionTicketQueue(
        "interactive",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        admission_ticket_queue=queue,
        admission_clock=lambda: clock[0],
    )

    first = _submit(app, "run-ticket-1")
    duplicate = _submit(app, "run-ticket-1")
    second = _submit(app, "run-ticket-2")

    assert first["admissionTicket"]["state"] == "admitted"
    assert duplicate["duplicate"] is True
    assert (
        duplicate["admissionTicket"]["ticketId"]
        == first["admissionTicket"]["ticketId"]
    )
    assert second["admissionTicket"] == {
        "ticketId": "interactive-ticket-000002",
        "runId": "run-ticket-2",
        "limiterId": "interactive",
        "state": "queued",
        "units": 1,
        "sequence": 2,
        "stateVersion": 1,
        "issuedAtUnixMs": 0,
        "expiresAtUnixMs": 60_000,
        "queuePosition": 1,
        "retryAfterMs": None,
        "startedAtUnixMs": None,
        "completedAtUnixMs": None,
    }
    assert app._events_by_run_id["run-ticket-1"] == ()
    assert app._events_by_run_id["run-ticket-2"] == ()

    status_response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-ticket-2",
            headers={},
            query={},
            cookies={},
        )
    )
    status = json.loads(status_response.body)
    assert status["state"] == "queued"
    assert status["startedAt"] is None
    assert status["lastCursor"] == "run-ticket-2:0"
    assert status["waitingOn"] == [
        {
            "kind": "admission",
            "ticketId": "interactive-ticket-000002",
            "limiterId": "interactive",
        }
    ]
    with pytest.raises(ValueError, match="queued for admission"):
        app.advance_accepted_run("run-ticket-2")

    first_completion = app.advance_accepted_run("run-ticket-1")

    assert first_completion["status"] == "succeeded"
    assert queue.get("interactive-ticket-000002").state == "admitted"
    assert app._events_by_run_id["run-ticket-2"] == ()

    second_completion = app.advance_accepted_run("run-ticket-2")

    assert second_completion["status"] == "succeeded"
    assert [
        event["kind"] for event in app._events_by_run_id["run-ticket-2"]
    ] == ["RunStarted", "RunSucceeded"]
    assert queue.get("interactive-ticket-000002").state == "completed"


def test_executor_never_runs_more_blocks_than_ticket_capacity() -> None:
    first_started = Event()
    release_first = Event()
    calls_lock = Lock()
    calls: list[str] = []
    active = 0
    peak_active = 0

    def mocked_external_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        nonlocal active, peak_active
        run_id = str(context["run_id"])
        with calls_lock:
            calls.append(run_id)
            active += 1
            peak_active = max(peak_active, active)
        if run_id == "run-external-1":
            first_started.set()
            assert release_first.wait(timeout=5)
        with calls_lock:
            active -= 1
        return {"value": f"mocked:{run_id}"}

    registry = RuntimeRegistry()
    registry.register("test.mocked-external@1", mocked_external_block)
    queue = AdmissionTicketQueue(
        "external-api",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        app = GraphBlocksServerApp(
            registry=registry,
            accepted_run_executor=executor,
            admission_ticket_queue=queue,
            admission_clock=lambda: 0,
        )
        _submit(app, "run-external-1", graph=_graph("test.mocked-external@1"))
        assert first_started.wait(timeout=5)
        second = _submit(
            app,
            "run-external-2",
            graph=_graph("test.mocked-external@1"),
        )

        assert second["admissionTicket"]["state"] == "queued"
        assert calls == ["run-external-1"]
        assert app._events_by_run_id["run-external-2"] == ()

        release_first.set()
        assert app.wait_for_accepted_run("run-external-1", timeout=5)["status"] == "succeeded"
        assert app.wait_for_accepted_run("run-external-2", timeout=5)["status"] == "succeeded"

    assert calls == ["run-external-1", "run-external-2"]
    assert peak_active == 1


def test_cancelling_queued_ticket_never_executes_it() -> None:
    queue = AdmissionTicketQueue(
        "cancel",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )
    _submit(app, "run-cancel-1")
    _submit(app, "run-cancel-2")

    cancelled = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-cancel-2/cancel",
            headers={},
            query={},
            cookies={},
            body=json.dumps({"reason": "client left"}).encode(),
            requested_at="2026-07-10T00:00:01Z",
        )
    )

    assert cancelled.status_code == 202
    assert queue.get("cancel-ticket-000002").state == "cancelled"
    assert [
        event["kind"] for event in app._events_by_run_id["run-cancel-2"]
    ] == ["RunCancelled"]
    assert app.advance_accepted_run("run-cancel-2")["status"] == "cancelled"


def test_maintenance_expires_queued_run_without_starting_it() -> None:
    clock = [0]
    queue = AdmissionTicketQueue(
        "ttl",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=100,
    )
    app = GraphBlocksServerApp(
        admission_ticket_queue=queue,
        admission_clock=lambda: clock[0],
    )
    _submit(app, "run-ttl-active")
    _submit(app, "run-ttl-expired")

    clock[0] = 100
    assert app.promote_admission_tickets() == ()

    assert queue.get("ttl-ticket-000002").state == "expired"
    assert [
        event["kind"] for event in app._events_by_run_id["run-ttl-expired"]
    ] == ["RunExpired"]
    assert "run-ttl-expired" not in app.pending_accepted_run_ids()
    status = json.loads(
        app.handle(
            ServerRequest(
                method="GET",
                path="/runs/run-ttl-expired",
                headers={},
                query={},
                cookies={},
            )
        ).body
    )
    assert status["state"] == "expired"
    assert status["startedAt"] is None
