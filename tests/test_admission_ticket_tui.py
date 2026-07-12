from __future__ import annotations

import importlib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


@pytest.fixture
def graphblocks_tui(monkeypatch):
    return importlib.import_module("graphblocks.tui")


def test_admission_ticket_screen_projects_queued_ticket(graphblocks_tui) -> None:
    screen = graphblocks_tui.admission_ticket_screen(
        {
            "ticketId": "ticket-queue-1",
            "runId": "run-1",
            "state": "queued",
            "limiterId": "interactive-sessions",
            "queueName": "interactive",
            "queuePosition": 3,
            "retryAfterMs": 2_500,
        }
    )

    assert screen.screen_contract() == {
        "name": "admission-ticket",
        "title": "Admission ticket ticket-queue-1",
        "sections": [
            {
                "title": "Ticket",
                "rows": {
                    "limiter_id": "interactive-sessions",
                    "ticket_id": "ticket-queue-1",
                },
            },
            {
                "title": "Run",
                "rows": {"run_id": "run-1", "state": "queued"},
            },
            {
                "title": "Queue",
                "rows": {
                    "name": "interactive",
                    "position": "3",
                    "retry_after_ms": "2500",
                },
            },
        ],
        "commands": [
            {"label": "Refresh", "action": "refresh", "key": "r"},
            {"label": "Cancel", "action": "cancel", "key": "c"},
        ],
    }


@pytest.mark.parametrize("run_state", ["admitted", "running"])
def test_admission_ticket_screen_projects_admitted_ticket(graphblocks_tui, run_state: str) -> None:
    screen = graphblocks_tui.admission_ticket_screen(
        {
            "ticketId": "ticket-admitted-1",
            "runId": "run-1",
            "state": run_state,
            "limiterId": "interactive-sessions",
            "queuePosition": None,
            "retryAfterMs": None,
        }
    )

    assert screen.screen_contract() == {
        "name": "admission-ticket",
        "title": "Admission ticket ticket-admitted-1",
        "sections": [
            {
                "title": "Ticket",
                "rows": {
                    "limiter_id": "interactive-sessions",
                    "ticket_id": "ticket-admitted-1",
                },
            },
            {"title": "Run", "rows": {"run_id": "run-1", "state": run_state}},
        ],
        "commands": [
            {"label": "Refresh", "action": "refresh", "key": "r"},
            {"label": "Cancel", "action": "cancel", "key": "c"},
        ],
    }


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "expired"])
def test_admission_ticket_screen_accepts_terminal_states(graphblocks_tui, state: str) -> None:
    screen = graphblocks_tui.admission_ticket_screen(
        {"ticketId": "ticket-terminal-1", "runId": "run-1", "state": state}
    )

    assert screen.screen_contract()["sections"][1] == {
        "title": "Run",
        "rows": {"run_id": "run-1", "state": state},
    }


@pytest.mark.parametrize(
    ("ticket", "message"),
    [
        ([], "admission ticket must be a mapping"),
        ({}, "admission ticket ticketId must be a non-empty string"),
        (
            {"ticketId": "ticket-1", "state": "queued"},
            "admission ticket runId must be a non-empty string",
        ),
        (
            {"ticketId": "ticket-1", "runId": "run-1"},
            "admission ticket state must be queued, admitted, running, completed, failed, cancelled, or expired",
        ),
        (
            {"ticketId": "ticket-1", "runId": "run-1", "state": "waiting"},
            "admission ticket state must be queued, admitted, running, completed, failed, cancelled, or expired",
        ),
        (
            {
                "ticketId": "ticket-1",
                "runId": "run-1",
                "state": "queued",
                "retryAfterMs": True,
            },
            "admission ticket retryAfterMs must be a non-negative integer",
        ),
        (
            {
                "ticketId": "ticket-1",
                "runId": "run-1",
                "state": "queued",
                "queuePosition": 0,
            },
            "admission ticket queuePosition must be a positive integer",
        ),
        (
            {
                "ticketId": "ticket-1",
                "runId": "run-1",
                "state": "running",
                "queueName": " ",
            },
            "admission ticket queueName must be a non-empty string",
        ),
        (
            {
                "ticketId": "ticket-1",
                "runId": "run-1",
                "state": "admitted",
                "limiterId": " ",
            },
            "admission ticket limiterId must be a non-empty string",
        ),
    ],
)
def test_admission_ticket_screen_rejects_malformed_contracts(
    graphblocks_tui,
    ticket: object,
    message: str,
) -> None:
    with pytest.raises(graphblocks_tui.TuiContractError, match=message):
        graphblocks_tui.admission_ticket_screen(ticket)
