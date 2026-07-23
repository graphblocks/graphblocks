from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from threading import Barrier, BrokenBarrierError, Lock

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


def test_budget_records_are_immutable_and_reject_ambiguous_accounting_keys() -> None:
    amount = _tokens("1")
    account = BudgetAccount(
        "budget-1",
        ResourceRef("tenant:acme"),
        [amount],
    )

    with pytest.raises(AttributeError):
        account.allocated.append(_tokens("2"))
    with pytest.raises(ValueError, match="surrounding whitespace"):
        UsageAmount(" model_total_tokens", Decimal("1"), "tokens")
    with pytest.raises(ValueError, match="dimensions must be string keys and values"):
        UsageAmount(
            "model_total_tokens",
            Decimal("1"),
            "tokens",
            dimensions={"model ": "small"},
        )
    with pytest.raises(ValueError, match="supported integer range"):
        BudgetAccount(
            "budget-overflow",
            ResourceRef("tenant:acme"),
            [amount],
            revision=1 << 64,
        )


def test_completion_reserve_rejects_inconsistent_restored_lifecycle() -> None:
    with pytest.raises(ValueError, match="spent completion reserve requires"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="finalization",
            amounts=[_tokens("1")],
            spendable_by=frozenset(("worker.finalize",)),
            status="spent",
        )
    with pytest.raises(ValueError, match="unspent completion reserve"):
        CompletionReserve(
            "reserve-1",
            "budget-1",
            purpose="finalization",
            amounts=[_tokens("1")],
            spendable_by=frozenset(("worker.finalize",)),
            reservation_id="reservation-1",
        )


def test_in_memory_budget_ledger_does_not_accept_unvalidated_restored_state() -> None:
    with pytest.raises(TypeError):
        InMemoryBudgetLedger(_reservation_counter=10)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("reserved", "reserved amounts must match active holds"),
        ("permit_authorization", "permit authorization must match"),
    ),
)
def test_sqlite_budget_ledger_reconciles_authoritative_snapshot_state(
    tmp_path,
    field: str,
    message: str,
) -> None:
    path = tmp_path / f"budget-reconcile-{field}.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    if field == "permit_authorization":
        ledger.issue_permit(
            "permit-1",
            reservation_ids=[reservation.reservation_id],
            owner=ResourceRef("worker:1"),
            atomic_unit=ResourceRef("turn:1"),
            admission_epoch=1,
            continuation_profile="finish_current_turn",
            policy_snapshot_digest="sha256:policy",
            expires_at="2026-06-22T01:00:00Z",
        )
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    if field == "reserved":
        snapshot["reserved"]["budget-1"] = []
    else:
        snapshot["permits"]["permit-1"]["authorized_amounts"][0]["amount"] = "100"
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match=message):
        SQLiteBudgetLedger(path)


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


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (
            lambda: BudgetAccount(
                " budget-1",
                ResourceRef("tenant:acme"),
                [_tokens("1")],
            ),
            "budget_id must not contain surrounding whitespace",
        ),
        (
            lambda: CompletionReserve(
                "reserve-1",
                "budget-1",
                "finalization",
                [_tokens("1")],
                frozenset({" worker.finalize"}),
            ),
            "spendable_by item must not contain surrounding whitespace",
        ),
    ),
)
def test_budget_records_reject_whitespace_wrapped_identities(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


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


def test_in_memory_budget_ledger_serializes_concurrent_reservations() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("10")], policy_ref="policy-1")
    reads = Barrier(2)
    read_count = 0
    read_count_lock = Lock()

    class CoordinatedReserved(dict):
        def get(self, key, default=None):
            nonlocal read_count
            value = super().get(key, default)
            with read_count_lock:
                should_wait = read_count < 2
                read_count += 1
            if should_wait:
                try:
                    reads.wait(timeout=0.2)
                except BrokenBarrierError:
                    pass
            return value

    ledger._reserved["budget-1"] = CoordinatedReserved(ledger._reserved["budget-1"])

    def reserve(owner_id: str) -> str:
        try:
            ledger.reserve(
                "budget-1",
                ResourceRef(owner_id),
                [_tokens("10")],
                purpose="provider_call",
                expires_at="later",
            )
        except BudgetExceededError:
            return "exceeded"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ("run:1", "run:2")))

    assert sorted(outcomes) == ["exceeded", "reserved"]
    assert ledger.balance("budget-1").reserved == [_tokens("10")]


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


def test_sqlite_budget_ledger_rejects_duplicate_snapshot_json_keys_on_replay(tmp_path) -> None:
    path = tmp_path / "budget-duplicate-key.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    state_json = row["state_json"]
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (state_json.replace('{"accounts":', '{"version":1,"accounts":', 1), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match="budget ledger state_json must be valid strict JSON"):
        SQLiteBudgetLedger(path)


def test_sqlite_budget_ledger_wraps_excessively_nested_snapshot_json(
    tmp_path,
) -> None:
    path = tmp_path / "budget-deeply-nested.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    deeply_nested = ("[" * 1_100) + "0" + ("]" * 1_100)
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? "
        "WHERE snapshot_id = ?",
        (deeply_nested, "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(
        ValueError,
        match="budget ledger state_json must be valid strict JSON",
    ):
        SQLiteBudgetLedger(path)


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    (
        ("version", True, "snapshot version must be integer 1"),
        ("reservation_counter", 1.5, "reservation_counter must be a non-negative integer"),
        ("account_id", 7, "budget account budget_id must be a string"),
        ("account_revision", True, "budget account revision must be an integer"),
        ("allocated_amount", 7, "usage amount values must be decimal strings"),
        ("account_key_mismatch", "budget-2", "accounts key must match budget_id"),
        (
            "aggregate_key_mismatch",
            None,
            "reserved keys must match account ids",
        ),
        (
            "account_allocation_mismatch",
            "99",
            "account allocated amounts must match allocated state",
        ),
    ),
)
def test_sqlite_budget_ledger_rejects_coerced_or_inconsistent_snapshot_fields(
    tmp_path,
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    path = tmp_path / f"budget-invalid-{field}.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    if field == "version":
        snapshot["version"] = invalid_value
    elif field == "reservation_counter":
        snapshot["reservation_counter"] = invalid_value
    elif field == "account_id":
        snapshot["accounts"]["budget-1"]["budget_id"] = invalid_value
    elif field == "account_revision":
        snapshot["accounts"]["budget-1"]["revision"] = invalid_value
    elif field == "allocated_amount":
        snapshot["accounts"]["budget-1"]["allocated"][0]["amount"] = invalid_value
    elif field == "aggregate_key_mismatch":
        snapshot["reserved"].pop("budget-1")
    elif field == "account_allocation_mismatch":
        snapshot["accounts"]["budget-1"]["allocated"][0]["amount"] = invalid_value
    else:
        snapshot["accounts"]["budget-1"]["budget_id"] = invalid_value
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match=message):
        SQLiteBudgetLedger(path)


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("fencing_counter", "fencing_counter precedes persisted fencing tokens"),
        ("reservation_holds", "reservation holds must match budget hierarchy"),
    ),
)
def test_sqlite_budget_ledger_rejects_inconsistent_restored_counters_and_holds(
    tmp_path,
    field: str,
    message: str,
) -> None:
    path = tmp_path / f"budget-inconsistent-{field}.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("10")],
        purpose="task",
        expires_at="2026-06-22T01:00:00Z",
    )
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    if field == "reservation_holds":
        snapshot["reservation_holds"][reservation.reservation_id] = []
    else:
        snapshot[field] = 0
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match=message):
        SQLiteBudgetLedger(path)


def test_sqlite_budget_ledger_reopens_custom_numeric_reservation_id(tmp_path) -> None:
    path = tmp_path / "budget-custom-reservation.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("10")],
        purpose="task",
        expires_at="2026-06-22T01:00:00Z",
        reservation_id="reservation-999999",
    )
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    snapshot["reservation_counter"] = 1
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    generated = reopened.reserve(
        "budget-1",
        ResourceRef("run:2"),
        [_tokens("10")],
        purpose="task",
        expires_at="2026-06-22T01:00:00Z",
    )

    assert generated.reservation_id == "reservation-1000000"
    reopened.close()


def test_sqlite_budget_ledger_rejects_non_finite_amount_map_on_replay(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    snapshot["allocated"]["budget-1"][0]["amount"] = "NaN"
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match="budget ledger amount map values must be finite"):
        SQLiteBudgetLedger(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({}, "amount maps must be arrays"),
        ([None], "amount map entries must be objects"),
        (
            [{"kind": 42, "unit": "tokens", "amount": "1", "dimensions": {}}],
            "amount map kinds must be non-empty strings",
        ),
        (
            [{"kind": "tokens", "unit": "tokens", "amount": "1", "dimensions": []}],
            "amount map dimensions must be objects",
        ),
    ),
)
def test_sqlite_budget_ledger_rejects_invalid_amount_map_shapes_on_replay(
    tmp_path,
    payload: object,
    message: str,
) -> None:
    path = tmp_path / "budget-invalid-amount-map.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    row = ledger._connection.execute(
        "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
        ("default",),
    ).fetchone()
    snapshot = json.loads(row["state_json"])
    snapshot["allocated"]["budget-1"] = payload
    ledger._connection.execute(
        "UPDATE budget_ledger_snapshots SET state_json = ? WHERE snapshot_id = ?",
        (json.dumps(snapshot), "default"),
    )
    ledger._connection.commit()
    ledger.close()

    with pytest.raises(ValueError, match=message):
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
