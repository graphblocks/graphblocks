from __future__ import annotations

import graphblocks
import pytest

from graphblocks.admission import (
    AdmissionIdempotencyConflictError,
    AdmissionQueueFullError,
    AdmissionStaleFencingTokenError,
    AdmissionTicketStateError,
    AdmissionTicketQueue,
)


def test_capacity_exhaustion_returns_ticket_and_completion_promotes_fifo() -> None:
    queue = AdmissionTicketQueue(
        "interactive",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    assert graphblocks.AdmissionTicketQueue is AdmissionTicketQueue

    first = queue.submit("run-1", "request-1", "user-1", now_ms=0)
    second = queue.submit("run-2", "request-2", "user-2", now_ms=0)
    third = queue.submit("run-3", "request-3", "user-3", now_ms=0)

    assert first.ticket.state == "admitted"
    assert first.ticket.fencing_token == 1
    assert first.duplicate is False
    assert second.ticket.state == "queued"
    assert second.ticket.queue_position == 1
    assert second.ticket.retry_after_ms is None
    assert third.ticket.queue_position == 2

    running = queue.mark_running(first.ticket.ticket_id, 1, now_ms=1)
    completed, promoted = queue.complete(
        running.ticket_id,
        1,
        "completed",
        now_ms=2,
    )

    assert completed.state == "completed"
    assert [ticket.run_id for ticket in promoted] == ["run-2"]
    assert promoted[0].state == "admitted"
    assert promoted[0].fencing_token == 2
    assert queue.get(third.ticket.ticket_id).queue_position == 1


def test_rate_limited_request_gets_ticket_with_retry_and_promotes_after_window() -> None:
    queue = AdmissionTicketQueue(
        "per-minute",
        max_concurrent=5,
        rate_limit=1,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    first = queue.submit("run-1", "request-1", "user-1", now_ms=0)
    queue.complete(first.ticket.ticket_id, 1, "completed", now_ms=1)

    limited = queue.submit("run-2", "request-2", "user-1", now_ms=10)

    assert limited.ticket.state == "queued"
    assert limited.ticket.retry_after_ms == 990
    assert queue.promote(now_ms=999) == ()
    promoted = queue.promote(now_ms=1_000)
    assert [ticket.run_id for ticket in promoted] == ["run-2"]
    assert promoted[0].retry_after_ms is None


def test_queued_retry_hints_include_earlier_queued_demand() -> None:
    queue = AdmissionTicketQueue(
        "projected",
        max_concurrent=1,
        rate_limit=2,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )

    queue.submit("run-1", "request-1", "user-1", now_ms=0)
    second = queue.submit("run-2", "request-2", "user-2", now_ms=0)
    third = queue.submit("run-3", "request-3", "user-3", now_ms=0)

    assert second.ticket.retry_after_ms is None
    assert third.ticket.retry_after_ms == 1_000


def test_admitted_ticket_expiry_releases_concurrency_slot() -> None:
    queue = AdmissionTicketQueue(
        "expiring",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=100,
    )
    admitted = queue.submit("run-1", "request-1", "user-1", now_ms=0).ticket
    waiting = queue.submit("run-2", "request-2", "user-2", now_ms=0).ticket

    expired = queue.expire(now_ms=100)
    promoted = queue.promote(now_ms=100)

    assert [ticket.ticket_id for ticket in expired] == [
        admitted.ticket_id,
        waiting.ticket_id,
    ]
    assert promoted == ()
    replacement = queue.submit("run-3", "request-3", "user-3", now_ms=100)
    assert replacement.ticket.state == "admitted"

    with pytest.raises(AdmissionTicketStateError, match="expired"):
        queue.mark_running(
            admitted.ticket_id,
            admitted.fencing_token or 0,
            now_ms=101,
        )


def test_submit_promotes_existing_fifo_ticket_after_admitted_expiry() -> None:
    queue = AdmissionTicketQueue(
        "expiring-fifo",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=100,
    )
    first = queue.submit("run-a", "request-a", "user-a", now_ms=0).ticket
    second = queue.submit("run-b", "request-b", "user-b", now_ms=1).ticket

    newcomer = queue.submit(
        "run-c",
        "request-c",
        "user-c",
        now_ms=100,
    ).ticket

    assert queue.get(first.ticket_id).state == "expired"
    promoted = queue.get(second.ticket_id)
    assert promoted.state == "admitted"
    assert promoted.fencing_token == 2
    assert newcomer.state == "queued"
    assert newcomer.queue_position == 1


def test_submit_does_not_bypass_rate_blocked_fifo_head_after_expiry() -> None:
    queue = AdmissionTicketQueue(
        "expiring-mixed-units",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=100,
    )
    first = queue.submit(
        "run-a",
        "request-a",
        "user-a",
        now_ms=0,
        units=6,
    ).ticket
    second = queue.submit(
        "run-b",
        "request-b",
        "user-b",
        now_ms=1,
        units=5,
    ).ticket

    newcomer = queue.submit(
        "run-c",
        "request-c",
        "user-c",
        now_ms=100,
        units=4,
    ).ticket

    assert queue.get(first.ticket_id).state == "expired"
    blocked_head = queue.get(second.ticket_id)
    assert blocked_head.state == "queued"
    assert blocked_head.queue_position == 1
    assert newcomer.state == "queued"
    assert newcomer.queue_position == 2


def test_submission_is_idempotent_without_double_charging_capacity() -> None:
    queue = AdmissionTicketQueue(
        "interactive",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )

    first = queue.submit("run-1", "request-1", "user-1", now_ms=0)
    duplicate = queue.submit("run-1", "request-1", "user-1", now_ms=100)
    queued = queue.submit("run-2", "request-2", "user-1", now_ms=100)

    assert duplicate.duplicate is True
    assert duplicate.ticket == first.ticket
    assert queued.ticket.state == "queued"
    with pytest.raises(AdmissionIdempotencyConflictError):
        queue.submit("run-other", "request-1", "user-1", now_ms=100)


def test_queue_limit_expiry_cancellation_and_fencing_fail_closed() -> None:
    queue = AdmissionTicketQueue(
        "bounded",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=1,
        ticket_ttl_ms=100,
    )
    active = queue.submit("run-1", "request-1", "user-1", now_ms=0)
    waiting = queue.submit("run-2", "request-2", "user-2", now_ms=0)

    with pytest.raises(AdmissionQueueFullError):
        queue.submit("run-3", "request-3", "user-3", now_ms=0)
    with pytest.raises(AdmissionStaleFencingTokenError):
        queue.complete(active.ticket.ticket_id, 99, "completed", now_ms=1)

    expired = queue.expire(now_ms=100)
    assert [ticket.ticket_id for ticket in expired] == [
        active.ticket.ticket_id,
        waiting.ticket.ticket_id,
    ]
    assert queue.get(active.ticket.ticket_id).state == "expired"
    assert queue.get(waiting.ticket.ticket_id).state == "expired"

    replacement = queue.submit("run-3", "request-3", "user-3", now_ms=100)
    assert replacement.ticket.state == "admitted"
    cancelled, promoted = queue.cancel(active.ticket.ticket_id, now_ms=101)
    assert cancelled.state == "expired"
    assert promoted == ()


def test_running_cancellation_requires_current_post_worker_fence() -> None:
    queue = AdmissionTicketQueue(
        "bounded",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=1,
        ticket_ttl_ms=100,
    )
    admitted = queue.submit("run-1", "request-1", "user-1", now_ms=0).ticket
    running = queue.mark_running(
        admitted.ticket_id,
        admitted.fencing_token or 0,
        now_ms=1,
    )

    with pytest.raises(AdmissionTicketStateError, match="post-worker fencing token"):
        queue.cancel(running.ticket_id, now_ms=2)
    with pytest.raises(AdmissionStaleFencingTokenError):
        queue.cancel(running.ticket_id, now_ms=2, fencing_token=99)
    cancelled, _ = queue.cancel(
        running.ticket_id,
        now_ms=3,
        fencing_token=running.fencing_token,
    )
    assert cancelled.state == "cancelled"


def test_ticket_contract_is_safe_for_client_screen_projection() -> None:
    queue = AdmissionTicketQueue(
        "interactive",
        max_concurrent=1,
        rate_limit=1,
        window_ms=1_000,
        max_pending=2,
        ticket_ttl_ms=5_000,
    )
    ticket = queue.submit("run-1", "request-1", "user-1", now_ms=25).ticket

    assert ticket.contract() == {
        "ticketId": "interactive-ticket-000001",
        "runId": "run-1",
        "limiterId": "interactive",
        "state": "admitted",
        "units": 1,
        "sequence": 1,
        "stateVersion": 1,
        "issuedAtUnixMs": 25,
        "expiresAtUnixMs": 5_025,
        "queuePosition": None,
        "retryAfterMs": None,
        "startedAtUnixMs": None,
        "completedAtUnixMs": None,
    }
