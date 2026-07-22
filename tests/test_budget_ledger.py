from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import graphblocks
from graphblocks.budget import (
    BudgetAccount,
    BudgetAccountStateError,
    BudgetCompletionReserveStateError,
    BudgetCompletionReserveUnauthorizedError,
    BudgetExceededError,
    BudgetReservation,
    BudgetSettlement,
    CompletionReserve,
    InMemoryBudgetLedger,
    BudgetReservationStateError,
    SQLiteBudgetLedger,
    UsageAmount,
    VALID_BUDGET_STATUSES,
    VALID_COMPLETION_RESERVE_PURPOSES,
    VALID_COMPLETION_RESERVE_STATUSES,
    VALID_RESERVATION_PURPOSES,
    VALID_RESERVATION_STATUSES,
)
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


def _set_budget_status(
    ledger: InMemoryBudgetLedger,
    budget_id: str,
    status: graphblocks.BudgetStatus,
) -> None:
    ledger._accounts[budget_id] = replace(ledger._accounts[budget_id], status=status)


def test_root_facade_exports_budget_literal_contract() -> None:
    expected_exports = {
        "BudgetStatus",
        "ReservationPurpose",
        "ReservationStatus",
        "CompletionReservePurpose",
        "CompletionReserveStatus",
        "VALID_BUDGET_STATUSES",
        "VALID_RESERVATION_PURPOSES",
        "VALID_RESERVATION_STATUSES",
        "VALID_COMPLETION_RESERVE_PURPOSES",
        "VALID_COMPLETION_RESERVE_STATUSES",
    }

    assert sorted(name for name in expected_exports if name not in graphblocks.__all__) == []
    for name in expected_exports:
        assert hasattr(graphblocks, name)
    assert graphblocks.VALID_BUDGET_STATUSES == VALID_BUDGET_STATUSES
    assert graphblocks.VALID_RESERVATION_PURPOSES == VALID_RESERVATION_PURPOSES
    assert graphblocks.VALID_RESERVATION_STATUSES == VALID_RESERVATION_STATUSES
    assert graphblocks.VALID_COMPLETION_RESERVE_PURPOSES == VALID_COMPLETION_RESERVE_PURPOSES
    assert graphblocks.VALID_COMPLETION_RESERVE_STATUSES == VALID_COMPLETION_RESERVE_STATUSES


def test_usage_amount_rejects_negative_amounts_and_freezes_dimensions() -> None:
    dimensions = {"model": "support"}

    amount = UsageAmount("model_total_tokens", Decimal("5"), "tokens", dimensions)
    dimensions["model"] = "mutated"

    assert amount.dimensions == {"model": "support"}
    with pytest.raises(TypeError):
        amount.dimensions["model"] = "direct"
    for invalid_amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="usage amount must be finite"):
            UsageAmount("model_total_tokens", invalid_amount, "tokens")
    with pytest.raises(ValueError, match="usage amount must be a decimal"):
        UsageAmount("model_total_tokens", object(), "tokens")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage amount must be non-negative"):
        UsageAmount("model_total_tokens", Decimal("-1"), "tokens")
    with pytest.raises(ValueError, match="usage amount kind must not be empty"):
        UsageAmount("", Decimal("1"), "tokens")
    with pytest.raises(ValueError, match="usage amount unit must not be empty"):
        UsageAmount("model_total_tokens", Decimal("1"), " ")
    with pytest.raises(ValueError, match="usage amount kind must be a string"):
        UsageAmount(1, Decimal("1"), "tokens")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage amount unit must be a string"):
        UsageAmount("model_total_tokens", Decimal("1"), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage amount dimensions must be a mapping"):
        UsageAmount("model_total_tokens", Decimal("1"), "tokens", dimensions=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage amount dimensions must be string keys and values"):
        UsageAmount("model_total_tokens", Decimal("1"), "tokens", dimensions={" ": "support"})
    with pytest.raises(ValueError, match="usage amount dimensions must be string keys and values"):
        UsageAmount("model_total_tokens", Decimal("1"), "tokens", dimensions={"model": " "})
    with pytest.raises(ValueError, match="usage amount dimensions must be string keys and values"):
        UsageAmount("model_total_tokens", Decimal("1"), "tokens", dimensions={"model": object()})  # type: ignore[dict-item]


def test_budget_models_reject_unknown_typed_values() -> None:
    with pytest.raises(ValueError, match="unknown budget status"):
        BudgetAccount("budget-1", ResourceRef("tenant:acme"), [_tokens("1")], status="maybe")
    with pytest.raises(ValueError, match="unknown reservation purpose"):
        BudgetReservation(
            "reservation-1",
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("1")],
            purpose="maybe",
            expires_at="later",
            fencing_token=1,
        )
    with pytest.raises(ValueError, match="unknown reservation status"):
        BudgetReservation(
            "reservation-1",
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("1")],
            purpose="provider_call",
            expires_at="later",
            fencing_token=1,
            status="maybe",
        )
    with pytest.raises(ValueError, match="unknown reservation status"):
        BudgetSettlement("reservation-1", "budget-1", status="maybe")
    with pytest.raises(ValueError, match="unknown completion reserve purpose"):
        CompletionReserve("reserve-1", "budget-1", purpose="maybe", amounts=[_tokens("1")], spendable_by=frozenset())
    with pytest.raises(ValueError, match="unknown completion reserve status"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="finalization",
            amounts=[_tokens("1")],
            spendable_by=frozenset({"agent.finalize"}),
            status="maybe",
        )


def test_budget_records_validate_identity_nested_records_and_counters() -> None:
    with pytest.raises(ValueError, match="budget account budget_id must not be empty"):
        BudgetAccount(" ", ResourceRef("tenant:acme"), [_tokens("1")])
    with pytest.raises(ValueError, match="budget account scope must be a ResourceRef"):
        BudgetAccount("budget-1", "tenant:acme", [_tokens("1")])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="budget account allocated must contain UsageAmount records"):
        BudgetAccount("budget-1", ResourceRef("tenant:acme"), [object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="budget account revision must be non-negative"):
        BudgetAccount("budget-1", ResourceRef("tenant:acme"), [_tokens("1")], revision=-1)
    with pytest.raises(ValueError, match="budget account policy_ref must be a string"):
        BudgetAccount("budget-1", ResourceRef("tenant:acme"), [_tokens("1")], policy_ref=object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="budget reservation owner must be a ResourceRef"):
        BudgetReservation(
            "reservation-1",
            "budget-1",
            "run:1",  # type: ignore[arg-type]
            [_tokens("1")],
            purpose="provider_call",
            expires_at="later",
            fencing_token=1,
        )
    with pytest.raises(ValueError, match="budget reservation expires_at must not be empty"):
        BudgetReservation(
            "reservation-1",
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("1")],
            purpose="provider_call",
            expires_at=" ",
            fencing_token=1,
        )
    with pytest.raises(ValueError, match="budget reservation fencing_token must be non-negative"):
        BudgetReservation(
            "reservation-1",
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("1")],
            purpose="provider_call",
            expires_at="later",
            fencing_token=-1,
        )

    with pytest.raises(ValueError, match="budget balance committed must contain UsageAmount records"):
        graphblocks.BudgetBalance(
            "budget-1",
            allocated=[_tokens("1")],
            reserved=[],
            committed=[object()],  # type: ignore[list-item]
            available=[],
            overdraft=[],
            revision=1,
        )
    with pytest.raises(ValueError, match="budget settlement revision must be non-negative"):
        BudgetSettlement("reservation-1", "budget-1", revision=-1)
    with pytest.raises(ValueError, match="completion reserve spendable_by item must not be empty"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="cleanup",
            amounts=[_tokens("1")],
            spendable_by=frozenset({" "}),
        )
    with pytest.raises(ValueError, match="completion reserve spendable_by must not be empty"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="cleanup",
            amounts=[_tokens("1")],
            spendable_by=frozenset(),
        )
    with pytest.raises(ValueError, match="completion reserve fencing_token must be an integer"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="cleanup",
            amounts=[_tokens("1")],
            spendable_by=frozenset({"cleanup.worker"}),
            fencing_token=True,  # type: ignore[arg-type]
        )


def test_budget_ledger_reserve_reduces_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")

    reservation = ledger.reserve(
        "budget-1",
        owner=ResourceRef("run:1", resource_kind="run"),
        amounts=[_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:00:00Z",
    )

    balance = ledger.balance("budget-1")
    assert reservation.fencing_token == 1
    assert reservation.status == "reserved"
    assert balance.reserved == [_tokens("40")]
    assert balance.available == [_tokens("60")]
    assert balance.revision == 2


def test_budget_ledger_rejects_reservation_above_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("80")], purpose="provider_call", expires_at="later")

    with pytest.raises(BudgetExceededError):
        ledger.reserve("budget-1", ResourceRef("run:2"), [_tokens("30")], purpose="provider_call", expires_at="later")


@pytest.mark.parametrize("status", ("exhausted", "paused", "closed"))
def test_non_active_budget_rejects_new_reservations_and_completion_reserves(
    status: graphblocks.BudgetStatus,
) -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    _set_budget_status(ledger, "budget-1", status)

    with pytest.raises(BudgetAccountStateError) as reservation_error:
        ledger.reserve(
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("10")],
            purpose="provider_call",
            expires_at="later",
        )
    with pytest.raises(BudgetAccountStateError):
        ledger.create_completion_reserve(
            "completion-1",
            "budget-1",
            purpose="finalization",
            amounts=[_tokens("10")],
            spendable_by=("agent.finalize",),
        )

    assert reservation_error.value.budget_id == "budget-1"
    assert reservation_error.value.status == status
    assert ledger.balance("budget-1").reserved == []


def test_non_active_budget_rejects_commit_and_completion_reserve_spend() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("20")],
        purpose="provider_call",
        expires_at="later",
    )
    ledger.create_completion_reserve(
        "completion-1",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("10")],
        spendable_by=("agent.finalize",),
    )
    _set_budget_status(ledger, "budget-1", "paused")

    with pytest.raises(BudgetAccountStateError):
        ledger.commit(reservation.reservation_id, [_tokens("15")])
    with pytest.raises(BudgetAccountStateError):
        ledger.spend_completion_reserve(
            "completion-1",
            "agent.finalize",
            expires_at="later",
        )

    assert ledger.balance("budget-1").reserved == [_tokens("30")]
    assert ledger.balance("budget-1").committed == []
    assert ledger.completion_reserve("completion-1").status == "available"


def test_child_budget_operations_require_active_parent_chain() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("parent", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.allocate(
        "child",
        ResourceRef("project:alpha"),
        [_tokens("50")],
        policy_ref="policy-1",
        parent_budget_id="parent",
    )
    _set_budget_status(ledger, "parent", "paused")

    with pytest.raises(BudgetAccountStateError) as error:
        ledger.reserve(
            "child",
            ResourceRef("run:1"),
            [_tokens("10")],
            purpose="provider_call",
            expires_at="later",
        )

    assert error.value.budget_id == "parent"
    assert error.value.status == "paused"


def test_budget_ledger_commit_releases_unused_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("25")])

    balance = ledger.balance("budget-1")
    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert balance.reserved == []
    assert balance.committed == [_tokens("25")]
    assert balance.available == [_tokens("75")]


def test_budget_ledger_release_restores_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.release(reservation.reservation_id)

    assert settlement.released == [_tokens("40")]
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_budget_ledger_expire_restores_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.expire(reservation.reservation_id)

    balance = ledger.balance("budget-1")
    assert settlement.status == "expired"
    assert settlement.released == [_tokens("40")]
    assert balance.reserved == []
    assert balance.committed == []
    assert balance.available == [_tokens("100")]


def test_budget_ledger_expired_reservation_cannot_be_settled_or_authorize_permit() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    ledger.expire(reservation.reservation_id)

    with pytest.raises(BudgetReservationStateError):
        ledger.commit(reservation.reservation_id, [_tokens("1")])
    with pytest.raises(BudgetReservationStateError):
        ledger.release(reservation.reservation_id)
    with pytest.raises(BudgetReservationStateError):
        ledger.issue_permit(
            "permit-1",
            reservation_ids=[reservation.reservation_id],
            owner=ResourceRef("worker:1"),
            atomic_unit=ResourceRef("turn:1"),
            admission_epoch=1,
            continuation_profile="hard_stop",
            policy_snapshot_digest="sha256:policy",
            expires_at="later",
        )


def test_budget_ledger_commit_over_reserved_records_overdraft() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("50")])

    balance = ledger.balance("budget-1")
    assert settlement.overdraft == [_tokens("10")]
    assert balance.committed == [_tokens("50")]
    assert balance.overdraft == [_tokens("10")]
    assert balance.available == [_tokens("50")]


def test_budget_ledger_commit_allows_overdraft_within_limit() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("45")], max_overdraft=[_tokens("5")])

    assert settlement.overdraft == [_tokens("5")]
    assert ledger.balance("budget-1").committed == [_tokens("45")]


def test_budget_ledger_rejects_commit_above_overdraft_limit_without_mutating_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    with pytest.raises(BudgetExceededError):
        ledger.commit(reservation.reservation_id, [_tokens("46")], max_overdraft=[_tokens("5")])

    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.overdraft == []
    assert balance.available == [_tokens("60")]


def test_completion_reserve_holds_finalization_capacity_out_of_general_budget() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")

    reserve = ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    assert reserve.status == "available"
    assert reserve.fencing_token == 1
    assert ledger.balance("budget-1").reserved == [_tokens("20")]
    assert ledger.balance("budget-1").available == [_tokens("80")]
    with pytest.raises(BudgetExceededError):
        ledger.reserve("budget-1", ResourceRef("planner"), [_tokens("90")], purpose="task", expires_at="later")


def test_completion_reserve_can_be_spent_by_authorized_finalization_work() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    reservation = ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")
    reserve = ledger.completion_reserve("finalization-reserve")

    assert reservation.purpose == "finalization"
    assert reservation.amounts == [_tokens("20")]
    assert reservation.fencing_token == reserve.fencing_token
    assert reserve.status == "spent"
    settlement = ledger.commit(reservation.reservation_id, [_tokens("15")])
    balance = ledger.balance("budget-1")
    assert settlement.committed == [_tokens("15")]
    assert settlement.released == [_tokens("5")]
    assert balance.reserved == []
    assert balance.committed == [_tokens("15")]
    assert balance.available == [_tokens("85")]


def test_completion_reserve_release_restores_held_capacity() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    reserve = ledger.release_completion_reserve("finalization-reserve")

    assert reserve.status == "released"
    assert ledger.balance("budget-1").reserved == []
    assert ledger.balance("budget-1").available == [_tokens("100")]
    with pytest.raises(BudgetCompletionReserveStateError):
        ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")


def test_completion_reserve_expire_restores_held_capacity() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
        expires_at="2026-06-22T00:05:00Z",
    )

    reserve = ledger.expire_completion_reserve("finalization-reserve")

    assert reserve.status == "expired"
    assert ledger.balance("budget-1").reserved == []
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_completion_reserve_rejects_unauthorized_spender() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "cleanup-reserve",
        "budget-1",
        purpose="cleanup",
        amounts=[_tokens("10")],
        spendable_by=("cleanup.worker",),
    )

    with pytest.raises(BudgetCompletionReserveUnauthorizedError) as error:
        ledger.spend_completion_reserve("cleanup-reserve", "planner", expires_at="later")

    assert error.value.reserve_id == "cleanup-reserve"
    assert error.value.spender == "planner"
    assert ledger.completion_reserve("cleanup-reserve").status == "available"


def test_sqlite_budget_ledger_persists_reserved_balance_and_permit_spend(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1", resource_kind="run"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:00:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1", resource_kind="worker"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T02:00:00Z",
    )
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.balance("budget-1").reserved == [_tokens("40")]
    settlement = reopened.commit_with_permit_at(
        permit.permit_id,
        reservation.reservation_id,
        [_tokens("25")],
        now="2026-06-22T01:30:00Z",
    )

    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert reopened.balance("budget-1").available == [_tokens("75")]
    reopened.close()


def test_sqlite_budget_ledger_rejects_non_standard_snapshot_json_on_replay(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        ('{"version": NaN}', "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match="budget ledger state_json must be valid strict JSON"):
        SQLiteBudgetLedger(path)


def test_sqlite_budget_ledger_persists_completion_reserve_lifecycle(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reserve = ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
        expires_at="2026-06-22T00:05:00Z",
    )
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.completion_reserve("finalization-reserve") == reserve
    assert reopened.balance("budget-1").available == [_tokens("80")]
    reservation = reopened.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")
    assert reopened.completion_reserve("finalization-reserve").status == "spent"
    reopened.close()

    final = SQLiteBudgetLedger(path)
    assert final.completion_reserve("finalization-reserve").reservation_id == reservation.reservation_id
    assert final.commit(reservation.reservation_id, [_tokens("15")]).released == [_tokens("5")]
    assert final.balance("budget-1").available == [_tokens("85")]
    final.close()
