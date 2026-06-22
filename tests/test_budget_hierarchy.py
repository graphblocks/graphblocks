from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import BudgetExceededError, InMemoryBudgetLedger, UsageAmount
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


def test_hierarchical_budget_reservation_holds_child_and_parent_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("tenant-budget", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="tenant-policy")
    ledger.allocate(
        "run-budget",
        ResourceRef("run:1"),
        [_tokens("80")],
        policy_ref="run-policy",
        parent_budget_id="tenant-budget",
    )

    ledger.reserve("run-budget", ResourceRef("attempt:1"), [_tokens("70")], purpose="provider_call", expires_at="later")

    assert ledger.balance("run-budget").available == [_tokens("10")]
    assert ledger.balance("tenant-budget").available == [_tokens("30")]


def test_hierarchical_budget_reservation_rejects_when_parent_balance_is_insufficient() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("tenant-budget", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="tenant-policy")
    ledger.allocate(
        "run-budget",
        ResourceRef("run:1"),
        [_tokens("120")],
        policy_ref="run-policy",
        parent_budget_id="tenant-budget",
    )
    ledger.reserve("run-budget", ResourceRef("attempt:1"), [_tokens("80")], purpose="provider_call", expires_at="later")

    with pytest.raises(BudgetExceededError):
        ledger.reserve("run-budget", ResourceRef("attempt:2"), [_tokens("30")], purpose="provider_call", expires_at="later")


def test_hierarchical_budget_release_restores_child_and_parent_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("tenant-budget", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="tenant-policy")
    ledger.allocate(
        "run-budget",
        ResourceRef("run:1"),
        [_tokens("80")],
        policy_ref="run-policy",
        parent_budget_id="tenant-budget",
    )
    reservation = ledger.reserve(
        "run-budget",
        ResourceRef("attempt:1"),
        [_tokens("70")],
        purpose="provider_call",
        expires_at="later",
    )

    ledger.release(reservation.reservation_id)

    assert ledger.balance("run-budget").available == [_tokens("80")]
    assert ledger.balance("tenant-budget").available == [_tokens("100")]


def test_hierarchical_budget_commit_settles_child_and_parent_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("tenant-budget", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="tenant-policy")
    ledger.allocate(
        "run-budget",
        ResourceRef("run:1"),
        [_tokens("80")],
        policy_ref="run-policy",
        parent_budget_id="tenant-budget",
    )
    reservation = ledger.reserve(
        "run-budget",
        ResourceRef("attempt:1"),
        [_tokens("70")],
        purpose="provider_call",
        expires_at="later",
    )

    ledger.commit(reservation.reservation_id, [_tokens("55")])

    assert ledger.balance("run-budget").committed == [_tokens("55")]
    assert ledger.balance("tenant-budget").committed == [_tokens("55")]
    assert ledger.balance("run-budget").available == [_tokens("25")]
    assert ledger.balance("tenant-budget").available == [_tokens("45")]
