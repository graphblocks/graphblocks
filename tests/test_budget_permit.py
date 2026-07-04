from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import (
    BudgetExceededError,
    BudgetPermit,
    BudgetPermitExpiredError,
    BudgetPermitScopeError,
    BudgetReservationStateError,
    InMemoryBudgetLedger,
    UsageAmount,
)
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


def test_budget_ledger_issues_bounded_permit_from_reservations() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1", resource_kind="worker"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=3,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T01:00:00Z",
    )

    assert permit.permit_id == "permit-1"
    assert permit.reservation_refs == (reservation.reservation_id,)
    assert permit.authorized_amounts == [_tokens("40")]
    assert permit.fencing_tokens == {"budget-1": reservation.fencing_token}
    with pytest.raises(TypeError):
        permit.fencing_tokens["budget-1"] = 0
    assert permit.owner.resource_id == "worker:1"
    assert permit.atomic_unit.resource_id == "turn:1"
    assert permit.allows([_tokens("25")]) is True
    assert permit.allows([_tokens("41")]) is False


def test_budget_permit_rejects_invalid_fencing_tokens() -> None:
    base = {
        "permit_id": "permit-1",
        "reservation_refs": ("reservation-1",),
        "owner": ResourceRef("worker:1", resource_kind="worker"),
        "atomic_unit": ResourceRef("turn:1", resource_kind="turn"),
        "admission_epoch": 3,
        "authorized_amounts": [_tokens("40")],
        "continuation_profile": "finish_current_turn",
        "policy_snapshot_digest": "sha256:policy",
        "expires_at": "2026-06-22T01:00:00Z",
    }

    with pytest.raises(ValueError, match="budget permit fencing_tokens must be a mapping"):
        BudgetPermit(**base, fencing_tokens=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="budget permit fencing token references must be non-empty strings"):
        BudgetPermit(**base, fencing_tokens={" ": 1})
    with pytest.raises(ValueError, match="budget permit fencing token values must be positive integers"):
        BudgetPermit(**base, fencing_tokens={"budget-1": True})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="budget permit fencing token values must be positive integers"):
        BudgetPermit(**base, fencing_tokens={"budget-1": 0})
    with pytest.raises(ValueError, match="budget permit fencing token values must be positive integers"):
        BudgetPermit(**base, fencing_tokens={"budget-1": -1})
    with pytest.raises(ValueError, match="budget permit fencing_tokens must not be empty"):
        BudgetPermit(**base, fencing_tokens={})


def test_budget_permit_validates_identity_scope_and_authorization_records() -> None:
    base = {
        "permit_id": "permit-1",
        "reservation_refs": ("reservation-1",),
        "owner": ResourceRef("worker:1", resource_kind="worker"),
        "atomic_unit": ResourceRef("turn:1", resource_kind="turn"),
        "admission_epoch": 3,
        "authorized_amounts": [_tokens("40")],
        "continuation_profile": "finish_current_turn",
        "policy_snapshot_digest": "sha256:policy",
        "expires_at": "2026-06-22T01:00:00Z",
        "fencing_tokens": {"budget-1": 1},
    }

    with pytest.raises(ValueError, match="budget permit permit_id must not be empty"):
        BudgetPermit(**{**base, "permit_id": " "})
    with pytest.raises(ValueError, match="budget permit reservation_refs must be a collection of strings"):
        BudgetPermit(**{**base, "reservation_refs": "reservation-1"})
    with pytest.raises(ValueError, match="budget permit reservation_refs item must not be empty"):
        BudgetPermit(**{**base, "reservation_refs": ("reservation-1", " ")})
    with pytest.raises(ValueError, match="budget permit reservation_refs must not be empty"):
        BudgetPermit(**{**base, "reservation_refs": ()})
    with pytest.raises(ValueError, match="budget permit reservation_refs must not contain duplicates"):
        BudgetPermit(**{**base, "reservation_refs": ("reservation-1", "reservation-1")})
    with pytest.raises(ValueError, match="budget permit owner must be a ResourceRef"):
        BudgetPermit(**{**base, "owner": "worker:1"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="budget permit atomic_unit must be a ResourceRef"):
        BudgetPermit(**{**base, "atomic_unit": "turn:1"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="budget permit admission_epoch must be non-negative"):
        BudgetPermit(**{**base, "admission_epoch": -1})
    with pytest.raises(ValueError, match="budget permit authorized_amounts must contain UsageAmount records"):
        BudgetPermit(**{**base, "authorized_amounts": [object()]})  # type: ignore[list-item]
    with pytest.raises(ValueError, match="budget permit continuation_profile must not be empty"):
        BudgetPermit(**{**base, "continuation_profile": " "})
    with pytest.raises(ValueError, match="budget permit policy_snapshot_digest must not be empty"):
        BudgetPermit(**{**base, "policy_snapshot_digest": ""})
    with pytest.raises(ValueError, match="budget permit expires_at must not be empty"):
        BudgetPermit(**{**base, "expires_at": " "})
    with pytest.raises(ValueError, match="budget permit low_watermark must contain UsageAmount records"):
        BudgetPermit(**base, low_watermark=[object()])  # type: ignore[list-item]


def test_budget_permit_requires_matching_usage_dimensions() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("100"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ],
        policy_ref="policy-1",
    )
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("40"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ],
        purpose="provider_call",
        expires_at="later",
    )
    issued = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    assert issued.allows(
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("20"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ]
    )
    assert not issued.allows(
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("20"),
                unit="tokens",
                dimensions={"model": "large"},
            )
        ]
    )


def test_budget_ledger_permit_combines_multiple_reservations() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    first = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("25")], purpose="task", expires_at="later")
    second = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("15")], purpose="finalization", expires_at="later")

    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[first.reservation_id, second.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="hard_stop",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    assert permit.authorized_amounts == [_tokens("40")]
    assert permit.fencing_tokens == {"budget-1": second.fencing_token}


def test_budget_ledger_rejects_permit_for_released_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")
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


def test_budget_ledger_commit_with_permit_settles_authorized_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    settlement = ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [_tokens("25")])

    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert ledger.balance("budget-1").available == [_tokens("75")]


def test_budget_ledger_release_with_permit_restores_authorized_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    settlement = ledger.release_with_permit(permit.permit_id, reservation.reservation_id)

    assert settlement.released == [_tokens("40")]
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_budget_ledger_commit_with_permit_rejects_usage_above_authorized_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    with pytest.raises(BudgetExceededError):
        ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [_tokens("41")])

    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.available == [_tokens("60")]


def test_budget_ledger_returned_permit_mutation_does_not_expand_authorization() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    permit.authorized_amounts.append(_tokens("1000"))

    with pytest.raises(BudgetExceededError):
        ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [_tokens("41")])

    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.available == [_tokens("60")]


def test_budget_ledger_returned_reservation_mutation_does_not_corrupt_release() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )

    reservation.amounts.append(_tokens("60"))
    settlement = ledger.release(reservation.reservation_id)

    assert settlement.released == [_tokens("40")]
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_budget_ledger_commit_with_expired_permit_rejects_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T00:10:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T00:05:00Z",
    )

    with pytest.raises(BudgetPermitExpiredError) as error:
        ledger.commit_with_permit_at(
            permit.permit_id,
            reservation.reservation_id,
            [_tokens("25")],
            now="2026-06-22T00:05:00Z",
        )

    assert error.value.permit_id == "permit-1"
    assert error.value.expires_at == "2026-06-22T00:05:00Z"
    assert error.value.now == "2026-06-22T00:05:00Z"
    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.available == [_tokens("60")]


def test_budget_ledger_commit_with_permit_compares_expiration_as_datetime() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:10:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-21T20:00:00-05:00",
    )

    settlement = ledger.commit_with_permit_at(
        permit.permit_id,
        reservation.reservation_id,
        [_tokens("25")],
        now="2026-06-22T00:59:59Z",
    )

    assert settlement.committed == [_tokens("25")]

    expired_ledger = InMemoryBudgetLedger()
    expired_ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    expired_reservation = expired_ledger.reserve(
        "budget-1",
        ResourceRef("run:2"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:10:00Z",
    )
    expired_permit = expired_ledger.issue_permit(
        "permit-2",
        reservation_ids=[expired_reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-21T20:00:00-05:00",
    )

    with pytest.raises(BudgetPermitExpiredError) as error:
        expired_ledger.commit_with_permit_at(
            expired_permit.permit_id,
            expired_reservation.reservation_id,
            [_tokens("25")],
            now="2026-06-22T01:00:01Z",
        )

    assert error.value.permit_id == "permit-2"
    assert error.value.expires_at == "2026-06-21T20:00:00-05:00"
    assert error.value.now == "2026-06-22T01:00:01Z"


def test_budget_ledger_release_with_expired_permit_rejects_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T00:10:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T00:05:00Z",
    )

    with pytest.raises(BudgetPermitExpiredError) as error:
        ledger.release_with_permit_at(
            permit.permit_id,
            reservation.reservation_id,
            now="2026-06-22T00:05:00Z",
        )

    assert error.value.permit_id == "permit-1"
    assert ledger.balance("budget-1").reserved == [_tokens("40")]


def test_budget_ledger_permit_cannot_settle_unreferenced_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    first = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("25")], purpose="task", expires_at="later")
    second = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("15")], purpose="task", expires_at="later")
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[first.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    with pytest.raises(BudgetPermitScopeError) as error:
        ledger.commit_with_permit(permit.permit_id, second.reservation_id, [_tokens("10")])

    assert error.value.permit_id == "permit-1"
    assert error.value.reservation_id == second.reservation_id
