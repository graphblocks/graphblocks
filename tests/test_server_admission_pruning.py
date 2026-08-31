from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from graphblocks.admission import (
    AdmissionTicketNotFoundError,
    AdmissionTicketQueue,
)
from graphblocks.server import GraphBlocksServerApp, ServerRequest


def _graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "ticket-pruning"},
        "spec": {
            "nodes": {
                "work": {
                    "block": "prompt.render@1",
                    "config": {"template": "Pruned {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }


def _submit(app: GraphBlocksServerApp, run_id: str) -> dict[str, object]:
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": _graph(),
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


def _queue(ticket_ttl_ms: int = 100) -> AdmissionTicketQueue:
    return AdmissionTicketQueue(
        "pruning",
        max_concurrent=2,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=ticket_ttl_ms,
    )


def _completed_run_with_pruned_ticket(
    clock: list[int],
) -> tuple[GraphBlocksServerApp, AdmissionTicketQueue, str]:
    queue = _queue()
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: clock[0],
    )
    run_id = "run-pruned-ticket-1"
    accepted = _submit(app, run_id)
    ticket_id = accepted["admissionTicket"]["ticketId"]
    completion = app.advance_accepted_run(run_id)
    assert completion["status"] == "succeeded"
    assert queue.get(ticket_id).state == "completed"

    clock[0] = 1_000
    queue.expire(now_ms=clock[0])
    with pytest.raises(AdmissionTicketNotFoundError):
        queue.get(ticket_id)
    return app, queue, run_id


def test_run_status_survives_pruned_admission_ticket() -> None:
    app, _, run_id = _completed_run_with_pruned_ticket([0])

    response = app.handle(
        ServerRequest(
            method="GET",
            path=f"/runs/{run_id}",
            headers={},
            query={},
            cookies={},
        )
    )

    assert response.status_code == 200
    status = json.loads(response.body)
    assert status["state"] == "succeeded"
    assert isinstance(status["completedAt"], str)
    assert "admissionTicket" not in status


def test_delete_terminal_run_survives_pruned_admission_ticket() -> None:
    app, _, run_id = _completed_run_with_pruned_ticket([0])

    deleted = app.delete_terminal_run(
        run_id,
        principal=None,
        deleted_at="2027-01-01T00:00:00Z",
    )

    assert deleted["runId"] == run_id
    assert deleted["duplicate"] is False


def test_terminal_run_collection_survives_pruned_admission_ticket() -> None:
    app, _, run_id = _completed_run_with_pruned_ticket([0])

    collected = app.collect_expired_terminal_runs(
        now="2027-01-01T00:00:00Z",
    )

    assert collected == (run_id,)


def test_resubmit_after_pruned_ticket_reports_run_conflict() -> None:
    app, _, run_id = _completed_run_with_pruned_ticket([0])

    replay = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": _graph(),
                    "inputs": {"message": {"text": run_id}},
                    "runId": run_id,
                    "requestId": f"request-{run_id}",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-10T00:00:00Z",
                }
            ).encode(),
        )
    )

    assert replay.status_code == 409
    assert json.loads(replay.body)["error"] == f"run {run_id!r} already exists"


class _ParkedCheckpoint:
    checkpoint_id = "checkpoint-parked-1"
    wait_node = "wait"
    output_values: dict[str, object] = {}
    operation = {
        "operation_id": "operation-parked-1",
        "node_id": "wait",
        "attempt_id": "attempt-parked-1",
        "provider_operation_id": "provider-parked-1",
        "expected_schema": "schemas/Parked@1",
    }

    def to_json(self) -> dict[str, object]:
        return {"output_values": {}}


class _StaticWaitingRuntime:
    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            status="waiting_callback",
            outputs={},
            journal=SimpleNamespace(records=()),
            checkpoint=_ParkedCheckpoint(),
        )


def _parked_waiting_callback_run(
    *,
    queue: AdmissionTicketQueue | None = None,
) -> tuple[GraphBlocksServerApp, str]:
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )
    run_id = "run-parked-callback-1"
    _submit(app, run_id)
    execution = app._accepted_run_executions_by_run_id[run_id]
    execution.runtime = _StaticWaitingRuntime()  # type: ignore[assignment]
    waiting = app.advance_accepted_run(run_id)
    assert waiting["status"] == "waiting_callback"
    assert app._accepted_run_executions_by_run_id[run_id].checkpoint is not None
    return app, run_id


def test_cancel_releases_running_admission_ticket_for_parked_run() -> None:
    queue = _queue(ticket_ttl_ms=60_000)
    app, run_id = _parked_waiting_callback_run(queue=queue)
    ticket_id = app._admission_ticket_ids_by_run_id[run_id]
    assert queue.get(ticket_id).state == "running"

    cancelled = app.handle(
        ServerRequest(
            method="POST",
            path=f"/runs/{run_id}/cancel",
            headers={},
            query={},
            cookies={},
            body=json.dumps({"reason": "operator abort"}).encode(),
            requested_at="2027-01-01T00:00:00Z",
        )
    )

    assert cancelled.status_code == 202
    assert queue.get(ticket_id).state == "cancelled"
    assert app._events_by_run_id[run_id][-1]["kind"] == "RunCancelled"


def test_drain_completes_while_run_is_parked_waiting_callback() -> None:
    app, run_id = _parked_waiting_callback_run()

    assert app.drain(timeout=0) is True
    assert run_id in app._pending_accepted_runs_by_run_id
    assert app.close(timeout=0) is True
