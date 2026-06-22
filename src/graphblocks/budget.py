from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Literal

from .policy import ResourceRef


AmountKey = tuple[str, str, tuple[tuple[str, str], ...]]
BudgetStatus = Literal["active", "exhausted", "paused", "closed"]
ReservationPurpose = Literal["provider_call", "task", "trial", "tool", "finalization", "cleanup"]
ReservationStatus = Literal["reserved", "committed", "released", "expired"]


class BudgetError(RuntimeError):
    pass


class BudgetNotFoundError(BudgetError):
    pass


class BudgetConflictError(BudgetError):
    pass


class BudgetExceededError(BudgetError):
    pass


class BudgetReservationNotFoundError(BudgetError):
    pass


class BudgetReservationStateError(BudgetError):
    pass


@dataclass(frozen=True, slots=True)
class UsageAmount:
    kind: str
    amount: Decimal
    unit: str
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))


@dataclass(frozen=True, slots=True)
class BudgetAccount:
    budget_id: str
    scope: ResourceRef
    allocated: list[UsageAmount]
    parent_budget_id: str | None = None
    status: BudgetStatus = "active"
    policy_ref: str = ""
    revision: int = 0


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    budget_id: str
    owner: ResourceRef
    amounts: list[UsageAmount]
    purpose: ReservationPurpose
    expires_at: str
    fencing_token: int
    status: ReservationStatus = "reserved"


@dataclass(frozen=True, slots=True)
class BudgetBalance:
    budget_id: str
    allocated: list[UsageAmount]
    reserved: list[UsageAmount]
    committed: list[UsageAmount]
    available: list[UsageAmount]
    overdraft: list[UsageAmount]
    revision: int
    observed_at: str = ""


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    reservation_id: str
    budget_id: str
    committed: list[UsageAmount] = field(default_factory=list)
    released: list[UsageAmount] = field(default_factory=list)
    overdraft: list[UsageAmount] = field(default_factory=list)
    status: ReservationStatus = "committed"
    revision: int = 0


@dataclass(frozen=True, slots=True)
class BudgetPermit:
    permit_id: str
    reservation_refs: tuple[str, ...]
    owner: ResourceRef
    atomic_unit: ResourceRef
    admission_epoch: int
    authorized_amounts: list[UsageAmount]
    continuation_profile: str
    policy_snapshot_digest: str
    expires_at: str
    low_watermark: list[UsageAmount] = field(default_factory=list)
    fencing_tokens: dict[str, int] = field(default_factory=dict)


def _amount_key(amount: UsageAmount) -> AmountKey:
    return (amount.kind, amount.unit, tuple(sorted(amount.dimensions.items())))


def _amounts_to_dict(amounts: list[UsageAmount]) -> dict[AmountKey, Decimal]:
    values: dict[AmountKey, Decimal] = {}
    for amount in amounts:
        key = _amount_key(amount)
        values[key] = values.get(key, Decimal("0")) + amount.amount
    return {key: value for key, value in values.items() if value != 0}


def _dict_to_amounts(values: dict[AmountKey, Decimal]) -> list[UsageAmount]:
    amounts: list[UsageAmount] = []
    for kind, unit, dimensions in sorted(values):
        amount = values[(kind, unit, dimensions)]
        if amount != 0:
            amounts.append(UsageAmount(kind=kind, amount=amount, unit=unit, dimensions=dict(dimensions)))
    return amounts


@dataclass(slots=True)
class InMemoryBudgetLedger:
    _accounts: dict[str, BudgetAccount] = field(default_factory=dict)
    _allocated: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict)
    _reserved: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict)
    _committed: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict)
    _overdraft: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict)
    _reservations: dict[str, BudgetReservation] = field(default_factory=dict)
    _permits: dict[str, BudgetPermit] = field(default_factory=dict)
    _reservation_counter: int = 0
    _fencing_counter: int = 0

    def allocate(
        self,
        budget_id: str,
        scope: ResourceRef,
        amounts: list[UsageAmount],
        *,
        policy_ref: str,
        parent_budget_id: str | None = None,
    ) -> BudgetAccount:
        if budget_id in self._accounts:
            raise BudgetConflictError(f"budget {budget_id!r} already exists")
        allocated = _amounts_to_dict(amounts)
        account = BudgetAccount(
            budget_id=budget_id,
            parent_budget_id=parent_budget_id,
            scope=scope,
            allocated=_dict_to_amounts(allocated),
            policy_ref=policy_ref,
            revision=1,
        )
        self._accounts[budget_id] = account
        self._allocated[budget_id] = allocated
        self._reserved[budget_id] = {}
        self._committed[budget_id] = {}
        self._overdraft[budget_id] = {}
        return account

    def reserve(
        self,
        budget_id: str,
        owner: ResourceRef,
        amounts: list[UsageAmount],
        *,
        purpose: ReservationPurpose,
        expires_at: str,
        reservation_id: str | None = None,
    ) -> BudgetReservation:
        if budget_id not in self._accounts:
            raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
        requested = _amounts_to_dict(amounts)
        available = _amounts_to_dict(self.balance(budget_id).available)
        for key, amount in requested.items():
            if amount > available.get(key, Decimal("0")):
                raise BudgetExceededError(f"budget {budget_id!r} has insufficient available {key[0]} {key[1]}")
        self._reservation_counter += 1
        self._fencing_counter += 1
        actual_reservation_id = reservation_id or f"reservation-{self._reservation_counter:06d}"
        if actual_reservation_id in self._reservations:
            raise BudgetConflictError(f"reservation {actual_reservation_id!r} already exists")
        for key, amount in requested.items():
            self._reserved[budget_id][key] = self._reserved[budget_id].get(key, Decimal("0")) + amount
        self._accounts[budget_id] = replace(self._accounts[budget_id], revision=self._accounts[budget_id].revision + 1)
        reservation = BudgetReservation(
            reservation_id=actual_reservation_id,
            budget_id=budget_id,
            owner=owner,
            amounts=_dict_to_amounts(requested),
            purpose=purpose,
            expires_at=expires_at,
            fencing_token=self._fencing_counter,
        )
        self._reservations[actual_reservation_id] = reservation
        return reservation

    def commit(self, reservation_id: str, actual_amounts: list[UsageAmount]) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        reserved = _amounts_to_dict(reservation.amounts)
        actual = _amounts_to_dict(actual_amounts)
        released: dict[AmountKey, Decimal] = {}
        overdraft: dict[AmountKey, Decimal] = {}
        for key, amount in reserved.items():
            self._reserved[budget_id][key] = self._reserved[budget_id].get(key, Decimal("0")) - amount
            if self._reserved[budget_id][key] == 0:
                del self._reserved[budget_id][key]
            unused = amount - actual.get(key, Decimal("0"))
            if unused > 0:
                released[key] = unused
        for key, amount in actual.items():
            self._committed[budget_id][key] = self._committed[budget_id].get(key, Decimal("0")) + amount
            extra = amount - reserved.get(key, Decimal("0"))
            if extra > 0:
                overdraft[key] = extra
                self._overdraft[budget_id][key] = self._overdraft[budget_id].get(key, Decimal("0")) + extra
        self._accounts[budget_id] = replace(self._accounts[budget_id], revision=self._accounts[budget_id].revision + 1)
        updated = replace(reservation, status="committed")
        self._reservations[reservation_id] = updated
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            committed=_dict_to_amounts(actual),
            released=_dict_to_amounts(released),
            overdraft=_dict_to_amounts(overdraft),
            status="committed",
            revision=self._accounts[budget_id].revision,
        )

    def release(self, reservation_id: str) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        reserved = _amounts_to_dict(reservation.amounts)
        for key, amount in reserved.items():
            self._reserved[budget_id][key] = self._reserved[budget_id].get(key, Decimal("0")) - amount
            if self._reserved[budget_id][key] == 0:
                del self._reserved[budget_id][key]
        self._accounts[budget_id] = replace(self._accounts[budget_id], revision=self._accounts[budget_id].revision + 1)
        self._reservations[reservation_id] = replace(reservation, status="released")
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            released=_dict_to_amounts(reserved),
            status="released",
            revision=self._accounts[budget_id].revision,
        )

    def issue_permit(
        self,
        permit_id: str,
        *,
        reservation_ids: list[str],
        owner: ResourceRef,
        atomic_unit: ResourceRef,
        admission_epoch: int,
        continuation_profile: str,
        policy_snapshot_digest: str,
        expires_at: str,
        low_watermark: list[UsageAmount] | None = None,
    ) -> BudgetPermit:
        if permit_id in self._permits:
            raise BudgetConflictError(f"permit {permit_id!r} already exists")
        authorized: dict[AmountKey, Decimal] = {}
        fencing_tokens: dict[str, int] = {}
        for reservation_id in reservation_ids:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
            if reservation.status != "reserved":
                raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
            for key, amount in _amounts_to_dict(reservation.amounts).items():
                authorized[key] = authorized.get(key, Decimal("0")) + amount
            fencing_tokens[reservation.budget_id] = max(
                fencing_tokens.get(reservation.budget_id, 0),
                reservation.fencing_token,
            )
        permit = BudgetPermit(
            permit_id=permit_id,
            reservation_refs=tuple(reservation_ids),
            owner=owner,
            atomic_unit=atomic_unit,
            admission_epoch=admission_epoch,
            authorized_amounts=_dict_to_amounts(authorized),
            low_watermark=list(low_watermark or []),
            continuation_profile=continuation_profile,
            policy_snapshot_digest=policy_snapshot_digest,
            expires_at=expires_at,
            fencing_tokens=fencing_tokens,
        )
        self._permits[permit_id] = permit
        return permit

    def balance(self, budget_id: str) -> BudgetBalance:
        account = self._accounts.get(budget_id)
        if account is None:
            raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
        allocated = self._allocated[budget_id]
        reserved = self._reserved[budget_id]
        committed = self._committed[budget_id]
        keys = set(allocated) | set(reserved) | set(committed)
        available: dict[AmountKey, Decimal] = {}
        for key in keys:
            remaining = allocated.get(key, Decimal("0")) - reserved.get(key, Decimal("0")) - committed.get(
                key, Decimal("0")
            )
            if remaining > 0:
                available[key] = remaining
        return BudgetBalance(
            budget_id=budget_id,
            allocated=_dict_to_amounts(allocated),
            reserved=_dict_to_amounts(reserved),
            committed=_dict_to_amounts(committed),
            available=_dict_to_amounts(available),
            overdraft=_dict_to_amounts(self._overdraft[budget_id]),
            revision=account.revision,
        )
