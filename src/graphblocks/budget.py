from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Literal, TypeVar, cast, get_args

from .policy import ResourceRef


AmountKey = tuple[str, str, tuple[tuple[str, str], ...]]
BudgetLedgerResult = TypeVar("BudgetLedgerResult")
BudgetStatus = Literal["active", "exhausted", "paused", "closed"]
ReservationPurpose = Literal["provider_call", "task", "trial", "tool", "finalization", "cleanup"]
ReservationStatus = Literal["reserved", "committed", "released", "expired"]
CompletionReservePurpose = Literal["finalization", "checkpoint", "cleanup", "compensation"]
CompletionReserveStatus = Literal["available", "spent", "released", "expired"]

VALID_BUDGET_STATUSES = frozenset(get_args(BudgetStatus))
VALID_RESERVATION_PURPOSES = frozenset(get_args(ReservationPurpose))
VALID_RESERVATION_STATUSES = frozenset(get_args(ReservationStatus))
VALID_COMPLETION_RESERVE_PURPOSES = frozenset(get_args(CompletionReservePurpose))
VALID_COMPLETION_RESERVE_STATUSES = frozenset(get_args(CompletionReserveStatus))


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


class BudgetPermitNotFoundError(BudgetError):
    pass


class BudgetPermitScopeError(BudgetError):
    def __init__(self, permit_id: str, reservation_id: str) -> None:
        super().__init__(f"permit {permit_id!r} cannot settle reservation {reservation_id!r}")
        self.permit_id = permit_id
        self.reservation_id = reservation_id


class BudgetPermitFencingError(BudgetError):
    def __init__(self, permit_id: str, budget_id: str, required_token: int, actual_token: int | None) -> None:
        super().__init__(
            f"permit {permit_id!r} has stale fencing token for budget {budget_id!r}: "
            f"{actual_token!r} < {required_token!r}"
        )
        self.permit_id = permit_id
        self.budget_id = budget_id
        self.required_token = required_token
        self.actual_token = actual_token


class BudgetPermitExpiredError(BudgetError):
    def __init__(self, permit_id: str, expires_at: str, now: str) -> None:
        super().__init__(f"permit {permit_id!r} expired at {expires_at!r} before {now!r}")
        self.permit_id = permit_id
        self.expires_at = expires_at
        self.now = now


class BudgetCompletionReserveNotFoundError(BudgetError):
    pass


class BudgetCompletionReserveConflictError(BudgetError):
    pass


class BudgetCompletionReserveUnauthorizedError(BudgetError):
    def __init__(self, reserve_id: str, spender: str) -> None:
        super().__init__(f"completion reserve {reserve_id!r} cannot be spent by {spender!r}")
        self.reserve_id = reserve_id
        self.spender = spender


class BudgetCompletionReserveStateError(BudgetError):
    pass


@dataclass(frozen=True, slots=True)
class UsageAmount:
    kind: str
    amount: Decimal
    unit: str
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise ValueError("usage amount kind must be a string")
        if not self.kind.strip():
            raise ValueError("usage amount kind must not be empty")
        if not isinstance(self.unit, str):
            raise ValueError("usage amount unit must be a string")
        if not self.unit.strip():
            raise ValueError("usage amount unit must not be empty")
        if not isinstance(self.dimensions, Mapping):
            raise ValueError("usage amount dimensions must be a mapping")
        dimensions = dict(self.dimensions)
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            for key, value in dimensions.items()
        ):
            raise ValueError("usage amount dimensions must be string keys and values")
        amount = self.amount
        if not isinstance(self.amount, Decimal):
            amount = Decimal(str(self.amount))
            object.__setattr__(self, "amount", amount)
        if amount < 0:
            raise ValueError("usage amount must be non-negative")
        object.__setattr__(self, "dimensions", MappingProxyType(dimensions))


@dataclass(frozen=True, slots=True)
class BudgetAccount:
    budget_id: str
    scope: ResourceRef
    allocated: list[UsageAmount]
    parent_budget_id: str | None = None
    status: BudgetStatus = "active"
    policy_ref: str = ""
    revision: int = 0

    def __post_init__(self) -> None:
        if self.status not in VALID_BUDGET_STATUSES:
            raise ValueError(f"unknown budget status {self.status!r}")


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

    def __post_init__(self) -> None:
        if self.purpose not in VALID_RESERVATION_PURPOSES:
            raise ValueError(f"unknown reservation purpose {self.purpose!r}")
        if self.status not in VALID_RESERVATION_STATUSES:
            raise ValueError(f"unknown reservation status {self.status!r}")


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

    def __post_init__(self) -> None:
        if self.status not in VALID_RESERVATION_STATUSES:
            raise ValueError(f"unknown reservation status {self.status!r}")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "reservation_refs", tuple(self.reservation_refs))
        if not isinstance(self.fencing_tokens, Mapping):
            raise ValueError("budget permit fencing_tokens must be a mapping")
        fencing_tokens = dict(self.fencing_tokens)
        for reference, token in fencing_tokens.items():
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("budget permit fencing token references must be non-empty strings")
            if not isinstance(token, int) or isinstance(token, bool) or token < 0:
                raise ValueError("budget permit fencing token values must be non-negative integers")
        object.__setattr__(self, "fencing_tokens", MappingProxyType(fencing_tokens))

    def allows(self, amounts: list[UsageAmount]) -> bool:
        authorized = _amounts_to_dict(self.authorized_amounts)
        requested = _amounts_to_dict(amounts)
        return all(amount <= authorized.get(key, Decimal("0")) for key, amount in requested.items())

    def is_active_at(self, now: str) -> bool:
        def parse_datetime(value: str) -> datetime:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("datetime must be a non-empty string")
            normalized = value.strip()
            if normalized.endswith(("Z", "z")):
                normalized = f"{normalized[:-1]}+00:00"
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        try:
            return parse_datetime(self.expires_at) > parse_datetime(now)
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class CompletionReserve:
    reserve_id: str
    budget_id: str
    purpose: CompletionReservePurpose
    amounts: list[UsageAmount]
    spendable_by: frozenset[str]
    expires_at: str | None = None
    status: CompletionReserveStatus = "available"
    reservation_id: str | None = None
    fencing_token: int = 0

    def __post_init__(self) -> None:
        if self.purpose not in VALID_COMPLETION_RESERVE_PURPOSES:
            raise ValueError(f"unknown completion reserve purpose {self.purpose!r}")
        if self.status not in VALID_COMPLETION_RESERVE_STATUSES:
            raise ValueError(f"unknown completion reserve status {self.status!r}")
        object.__setattr__(self, "spendable_by", frozenset(self.spendable_by))


BudgetRecord = TypeVar("BudgetRecord", BudgetAccount, BudgetReservation, BudgetPermit, CompletionReserve)


def _copy_usage_amount(amount: UsageAmount) -> UsageAmount:
    return UsageAmount(
        kind=amount.kind,
        amount=amount.amount,
        unit=amount.unit,
        dimensions=dict(amount.dimensions),
    )


def _copy_usage_amounts(amounts: list[UsageAmount]) -> list[UsageAmount]:
    return [_copy_usage_amount(amount) for amount in amounts]


def _copy_resource_ref(resource: ResourceRef) -> ResourceRef:
    return ResourceRef(
        resource_id=resource.resource_id,
        resource_kind=resource.resource_kind,
        tenant_id=resource.tenant_id,
        attributes=dict(resource.attributes),
    )


def _copy_budget_record(record: BudgetRecord) -> BudgetRecord:
    if isinstance(record, BudgetAccount):
        return cast(
            BudgetRecord,
            BudgetAccount(
                budget_id=record.budget_id,
                scope=_copy_resource_ref(record.scope),
                allocated=_copy_usage_amounts(record.allocated),
                parent_budget_id=record.parent_budget_id,
                status=record.status,
                policy_ref=record.policy_ref,
                revision=record.revision,
            ),
        )
    if isinstance(record, BudgetReservation):
        return cast(
            BudgetRecord,
            BudgetReservation(
                reservation_id=record.reservation_id,
                budget_id=record.budget_id,
                owner=_copy_resource_ref(record.owner),
                amounts=_copy_usage_amounts(record.amounts),
                purpose=record.purpose,
                expires_at=record.expires_at,
                fencing_token=record.fencing_token,
                status=record.status,
            ),
        )
    if isinstance(record, BudgetPermit):
        return cast(
            BudgetRecord,
            BudgetPermit(
                permit_id=record.permit_id,
                reservation_refs=tuple(record.reservation_refs),
                owner=_copy_resource_ref(record.owner),
                atomic_unit=_copy_resource_ref(record.atomic_unit),
                admission_epoch=record.admission_epoch,
                authorized_amounts=_copy_usage_amounts(record.authorized_amounts),
                continuation_profile=record.continuation_profile,
                policy_snapshot_digest=record.policy_snapshot_digest,
                expires_at=record.expires_at,
                low_watermark=_copy_usage_amounts(record.low_watermark),
                fencing_tokens=dict(record.fencing_tokens),
            ),
        )
    if isinstance(record, CompletionReserve):
        return cast(
            BudgetRecord,
            CompletionReserve(
                reserve_id=record.reserve_id,
                budget_id=record.budget_id,
                purpose=record.purpose,
                amounts=_copy_usage_amounts(record.amounts),
                spendable_by=frozenset(record.spendable_by),
                expires_at=record.expires_at,
                status=record.status,
                reservation_id=record.reservation_id,
                fencing_token=record.fencing_token,
            ),
        )
    raise TypeError(f"unsupported budget record {type(record).__name__}")


def _reservation_purpose_for_completion_reserve(purpose: CompletionReservePurpose) -> ReservationPurpose:
    if purpose in {"finalization", "checkpoint"}:
        return "finalization"
    return "cleanup"


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
    _reservation_holds: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _permits: dict[str, BudgetPermit] = field(default_factory=dict)
    _permit_spent: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict)
    _completion_reserves: dict[str, CompletionReserve] = field(default_factory=dict)
    _completion_reserve_holds: dict[str, tuple[str, ...]] = field(default_factory=dict)
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
        if parent_budget_id is not None and parent_budget_id not in self._accounts:
            raise BudgetNotFoundError(f"parent budget {parent_budget_id!r} does not exist")
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
        return _copy_budget_record(account)

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
        held_budget_ids = self._budget_chain(budget_id)
        for held_budget_id in held_budget_ids:
            available = _amounts_to_dict(self.balance(held_budget_id).available)
            for key, amount in requested.items():
                if amount > available.get(key, Decimal("0")):
                    raise BudgetExceededError(
                        f"budget {held_budget_id!r} has insufficient available {key[0]} {key[1]}"
                    )
        self._reservation_counter += 1
        self._fencing_counter += 1
        actual_reservation_id = reservation_id or f"reservation-{self._reservation_counter:06d}"
        if actual_reservation_id in self._reservations:
            raise BudgetConflictError(f"reservation {actual_reservation_id!r} already exists")
        for held_budget_id in held_budget_ids:
            for key, amount in requested.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) + amount
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
        reservation = BudgetReservation(
            reservation_id=actual_reservation_id,
            budget_id=budget_id,
            owner=owner,
            amounts=_dict_to_amounts(requested),
            purpose=purpose,
            expires_at=expires_at,
            fencing_token=self._fencing_counter,
        )
        self._reservations[actual_reservation_id] = _copy_budget_record(reservation)
        self._reservation_holds[actual_reservation_id] = held_budget_ids
        return _copy_budget_record(reservation)

    def commit(
        self,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        held_budget_ids = self._reservation_holds.get(reservation_id, (budget_id,))
        reserved = _amounts_to_dict(reservation.amounts)
        actual = _amounts_to_dict(actual_amounts)
        if max_overdraft is not None:
            overdraft_limit = _amounts_to_dict(max_overdraft)
            for key, amount in actual.items():
                extra = amount - reserved.get(key, Decimal("0"))
                if extra > overdraft_limit.get(key, Decimal("0")):
                    raise BudgetExceededError(
                        f"reservation {reservation_id!r} overdraft exceeds allowed {key[0]} {key[1]}"
                    )
        released: dict[AmountKey, Decimal] = {}
        overdraft: dict[AmountKey, Decimal] = {}
        for held_budget_id in held_budget_ids:
            for key, amount in reserved.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
                unused = amount - actual.get(key, Decimal("0"))
                if unused > 0 and held_budget_id == budget_id:
                    released[key] = unused
            for key, amount in actual.items():
                self._committed[held_budget_id][key] = self._committed[held_budget_id].get(key, Decimal("0")) + amount
                extra = amount - reserved.get(key, Decimal("0"))
                if extra > 0:
                    if held_budget_id == budget_id:
                        overdraft[key] = extra
                    self._overdraft[held_budget_id][key] = self._overdraft[held_budget_id].get(key, Decimal("0")) + extra
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
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
        held_budget_ids = self._reservation_holds.get(reservation_id, (budget_id,))
        reserved = _amounts_to_dict(reservation.amounts)
        for held_budget_id in held_budget_ids:
            for key, amount in reserved.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
        self._reservations[reservation_id] = replace(reservation, status="released")
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            released=_dict_to_amounts(reserved),
            status="released",
            revision=self._accounts[budget_id].revision,
        )

    def expire(self, reservation_id: str) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        held_budget_ids = self._reservation_holds.get(reservation_id, (budget_id,))
        reserved = _amounts_to_dict(reservation.amounts)
        for held_budget_id in held_budget_ids:
            for key, amount in reserved.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
        self._reservations[reservation_id] = replace(reservation, status="expired")
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            released=_dict_to_amounts(reserved),
            status="expired",
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
            for held_budget_id in self._reservation_holds.get(reservation_id, (reservation.budget_id,)):
                fencing_tokens[held_budget_id] = max(
                    fencing_tokens.get(held_budget_id, 0),
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
        self._permits[permit_id] = _copy_budget_record(permit)
        self._permit_spent[permit_id] = {}
        return _copy_budget_record(permit)

    def commit_with_permit(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        permit = self._permit_for_reservation(permit_id, reservation_id)
        return self._commit_with_permit(
            permit,
            reservation_id,
            actual_amounts,
            max_overdraft=max_overdraft,
        )

    def commit_with_permit_at(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        now: str,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        permit = self._permit_for_reservation(permit_id, reservation_id)
        self._ensure_permit_not_expired(permit, now)
        return self._commit_with_permit(
            permit,
            reservation_id,
            actual_amounts,
            max_overdraft=max_overdraft,
        )

    def _commit_with_permit(
        self,
        permit: BudgetPermit,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        actual = _amounts_to_dict(actual_amounts)
        self._ensure_permit_allows_additional(permit, actual, self._reservations[reservation_id].budget_id)
        settlement = self.commit(reservation_id, actual_amounts, max_overdraft=max_overdraft)
        spent = self._permit_spent.setdefault(permit.permit_id, {})
        for key, amount in actual.items():
            spent[key] = spent.get(key, Decimal("0")) + amount
            if spent[key] == 0:
                del spent[key]
        return settlement

    def release_with_permit(self, permit_id: str, reservation_id: str) -> BudgetSettlement:
        self._permit_for_reservation(permit_id, reservation_id)
        return self.release(reservation_id)

    def release_with_permit_at(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        permit = self._permit_for_reservation(permit_id, reservation_id)
        self._ensure_permit_not_expired(permit, now)
        return self.release(reservation_id)

    def create_completion_reserve(
        self,
        reserve_id: str,
        budget_id: str,
        *,
        purpose: CompletionReservePurpose,
        amounts: list[UsageAmount],
        spendable_by: tuple[str, ...],
        expires_at: str | None = None,
    ) -> CompletionReserve:
        if reserve_id in self._completion_reserves:
            raise BudgetCompletionReserveConflictError(f"completion reserve {reserve_id!r} already exists")
        if budget_id not in self._accounts:
            raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
        requested = _amounts_to_dict(amounts)
        held_budget_ids = self._budget_chain(budget_id)
        for held_budget_id in held_budget_ids:
            available = _amounts_to_dict(self.balance(held_budget_id).available)
            for key, amount in requested.items():
                if amount > available.get(key, Decimal("0")):
                    raise BudgetExceededError(
                        f"budget {held_budget_id!r} has insufficient available {key[0]} {key[1]}"
                    )
        self._fencing_counter += 1
        for held_budget_id in held_budget_ids:
            for key, amount in requested.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) + amount
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
        reserve = CompletionReserve(
            reserve_id=reserve_id,
            budget_id=budget_id,
            purpose=purpose,
            amounts=_dict_to_amounts(requested),
            spendable_by=frozenset(spendable_by),
            expires_at=expires_at,
            fencing_token=self._fencing_counter,
        )
        self._completion_reserves[reserve_id] = _copy_budget_record(reserve)
        self._completion_reserve_holds[reserve_id] = held_budget_ids
        return _copy_budget_record(reserve)

    def completion_reserve(self, reserve_id: str) -> CompletionReserve:
        reserve = self._completion_reserves.get(reserve_id)
        if reserve is None:
            raise BudgetCompletionReserveNotFoundError(f"completion reserve {reserve_id!r} does not exist")
        return _copy_budget_record(reserve)

    def spend_completion_reserve(
        self,
        reserve_id: str,
        spender: str,
        *,
        expires_at: str,
    ) -> BudgetReservation:
        reserve = self._completion_reserves.get(reserve_id)
        if reserve is None:
            raise BudgetCompletionReserveNotFoundError(f"completion reserve {reserve_id!r} does not exist")
        if reserve.status != "available":
            raise BudgetCompletionReserveStateError(f"completion reserve {reserve_id!r} is {reserve.status}")
        if spender not in reserve.spendable_by:
            raise BudgetCompletionReserveUnauthorizedError(reserve_id, spender)

        self._reservation_counter += 1
        reservation_id = f"reservation-{self._reservation_counter:06d}"
        if reservation_id in self._reservations:
            raise BudgetConflictError(f"reservation {reservation_id!r} already exists")
        reservation = BudgetReservation(
            reservation_id=reservation_id,
            budget_id=reserve.budget_id,
            owner=ResourceRef(spender),
            amounts=list(reserve.amounts),
            purpose=_reservation_purpose_for_completion_reserve(reserve.purpose),
            expires_at=expires_at,
            fencing_token=reserve.fencing_token,
        )
        self._reservations[reservation_id] = _copy_budget_record(reservation)
        self._reservation_holds[reservation_id] = self._completion_reserve_holds.get(reserve_id, (reserve.budget_id,))
        self._completion_reserves[reserve_id] = replace(
            reserve,
            status="spent",
            reservation_id=reservation_id,
        )
        return _copy_budget_record(reservation)

    def release_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._settle_completion_reserve(reserve_id, "released")

    def expire_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._settle_completion_reserve(reserve_id, "expired")

    def _settle_completion_reserve(
        self,
        reserve_id: str,
        status: CompletionReserveStatus,
    ) -> CompletionReserve:
        reserve = self._completion_reserves.get(reserve_id)
        if reserve is None:
            raise BudgetCompletionReserveNotFoundError(f"completion reserve {reserve_id!r} does not exist")
        if reserve.status != "available":
            raise BudgetCompletionReserveStateError(f"completion reserve {reserve_id!r} is {reserve.status}")

        amounts = _amounts_to_dict(reserve.amounts)
        for held_budget_id in self._completion_reserve_holds.get(reserve_id, (reserve.budget_id,)):
            for key, amount in amounts.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=self._accounts[held_budget_id].revision + 1,
            )
        updated = replace(reserve, status=status)
        self._completion_reserves[reserve_id] = updated
        return _copy_budget_record(updated)

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

    def _budget_chain(self, budget_id: str) -> tuple[str, ...]:
        chain: list[str] = []
        seen: set[str] = set()
        current_id: str | None = budget_id
        while current_id is not None:
            if current_id in seen:
                raise BudgetConflictError(f"budget hierarchy cycle at {current_id!r}")
            account = self._accounts.get(current_id)
            if account is None:
                raise BudgetNotFoundError(f"budget {current_id!r} does not exist")
            chain.append(current_id)
            seen.add(current_id)
            current_id = account.parent_budget_id
        return tuple(chain)

    def _permit_for_reservation(self, permit_id: str, reservation_id: str) -> BudgetPermit:
        permit = self._permits.get(permit_id)
        if permit is None:
            raise BudgetPermitNotFoundError(f"permit {permit_id!r} does not exist")
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation_id not in permit.reservation_refs:
            raise BudgetPermitScopeError(permit_id, reservation_id)
        for budget_id in self._reservation_holds.get(reservation_id, (reservation.budget_id,)):
            actual_token = permit.fencing_tokens.get(budget_id)
            if actual_token is None or actual_token < reservation.fencing_token:
                raise BudgetPermitFencingError(permit_id, budget_id, reservation.fencing_token, actual_token)
        return permit

    def _ensure_permit_not_expired(self, permit: BudgetPermit, now: str) -> None:
        if not permit.is_active_at(now):
            raise BudgetPermitExpiredError(permit.permit_id, permit.expires_at, now)

    def _ensure_permit_allows_additional(
        self,
        permit: BudgetPermit,
        requested: dict[AmountKey, Decimal],
        budget_id: str,
    ) -> None:
        authorized = _amounts_to_dict(permit.authorized_amounts)
        spent = self._permit_spent.setdefault(permit.permit_id, {})
        for key, amount in requested.items():
            if spent.get(key, Decimal("0")) + amount > authorized.get(key, Decimal("0")):
                raise BudgetExceededError(
                    f"permit {permit.permit_id!r} exceeds authorized {key[0]} {key[1]} for budget {budget_id!r}"
                )


def _usage_amount_to_json(amount: UsageAmount) -> dict[str, object]:
    return {
        "kind": amount.kind,
        "amount": str(amount.amount),
        "unit": amount.unit,
        "dimensions": dict(sorted(amount.dimensions.items())),
    }


def _usage_amount_from_json(data: dict[str, object]) -> UsageAmount:
    return UsageAmount(
        kind=str(data["kind"]),
        amount=Decimal(str(data["amount"])),
        unit=str(data["unit"]),
        dimensions=dict(data.get("dimensions", {})),
    )


def _resource_ref_to_json(resource: ResourceRef) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "tenant_id": resource.tenant_id,
        "attributes": dict(sorted(resource.attributes.items())),
    }


def _resource_ref_from_json(data: dict[str, object]) -> ResourceRef:
    return ResourceRef(
        resource_id=str(data["resource_id"]),
        resource_kind=data.get("resource_kind"),
        tenant_id=data.get("tenant_id"),
        attributes=dict(data.get("attributes", {})),
    )


def _amount_map_to_json(values: dict[AmountKey, Decimal]) -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "amount": str(values[(kind, unit, dimensions)]),
            "unit": unit,
            "dimensions": dict(dimensions),
        }
        for kind, unit, dimensions in sorted(values)
    ]


def _amount_map_from_json(entries: list[dict[str, object]]) -> dict[AmountKey, Decimal]:
    values: dict[AmountKey, Decimal] = {}
    for entry in entries:
        dimensions = tuple(sorted(dict(entry.get("dimensions", {})).items()))
        values[(str(entry["kind"]), str(entry["unit"]), dimensions)] = Decimal(str(entry["amount"]))
    return values


def _account_to_json(account: BudgetAccount) -> dict[str, object]:
    return {
        "budget_id": account.budget_id,
        "scope": _resource_ref_to_json(account.scope),
        "allocated": [_usage_amount_to_json(amount) for amount in account.allocated],
        "parent_budget_id": account.parent_budget_id,
        "status": account.status,
        "policy_ref": account.policy_ref,
        "revision": account.revision,
    }


def _account_from_json(data: dict[str, object]) -> BudgetAccount:
    return BudgetAccount(
        budget_id=str(data["budget_id"]),
        scope=_resource_ref_from_json(data["scope"]),
        allocated=[_usage_amount_from_json(entry) for entry in data.get("allocated", [])],
        parent_budget_id=data.get("parent_budget_id"),
        status=data.get("status", "active"),
        policy_ref=str(data.get("policy_ref", "")),
        revision=int(data.get("revision", 0)),
    )


def _reservation_to_json(reservation: BudgetReservation) -> dict[str, object]:
    return {
        "reservation_id": reservation.reservation_id,
        "budget_id": reservation.budget_id,
        "owner": _resource_ref_to_json(reservation.owner),
        "amounts": [_usage_amount_to_json(amount) for amount in reservation.amounts],
        "purpose": reservation.purpose,
        "expires_at": reservation.expires_at,
        "fencing_token": reservation.fencing_token,
        "status": reservation.status,
    }


def _reservation_from_json(data: dict[str, object]) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=str(data["reservation_id"]),
        budget_id=str(data["budget_id"]),
        owner=_resource_ref_from_json(data["owner"]),
        amounts=[_usage_amount_from_json(entry) for entry in data.get("amounts", [])],
        purpose=data["purpose"],
        expires_at=str(data["expires_at"]),
        fencing_token=int(data.get("fencing_token", 0)),
        status=data.get("status", "reserved"),
    )


def _permit_to_json(permit: BudgetPermit) -> dict[str, object]:
    return {
        "permit_id": permit.permit_id,
        "reservation_refs": list(permit.reservation_refs),
        "owner": _resource_ref_to_json(permit.owner),
        "atomic_unit": _resource_ref_to_json(permit.atomic_unit),
        "admission_epoch": permit.admission_epoch,
        "authorized_amounts": [_usage_amount_to_json(amount) for amount in permit.authorized_amounts],
        "continuation_profile": permit.continuation_profile,
        "policy_snapshot_digest": permit.policy_snapshot_digest,
        "expires_at": permit.expires_at,
        "low_watermark": [_usage_amount_to_json(amount) for amount in permit.low_watermark],
        "fencing_tokens": dict(sorted(permit.fencing_tokens.items())),
    }


def _permit_from_json(data: dict[str, object]) -> BudgetPermit:
    return BudgetPermit(
        permit_id=str(data["permit_id"]),
        reservation_refs=tuple(str(reservation_id) for reservation_id in data.get("reservation_refs", [])),
        owner=_resource_ref_from_json(data["owner"]),
        atomic_unit=_resource_ref_from_json(data["atomic_unit"]),
        admission_epoch=int(data["admission_epoch"]),
        authorized_amounts=[_usage_amount_from_json(entry) for entry in data.get("authorized_amounts", [])],
        continuation_profile=str(data["continuation_profile"]),
        policy_snapshot_digest=str(data["policy_snapshot_digest"]),
        expires_at=str(data["expires_at"]),
        low_watermark=[_usage_amount_from_json(entry) for entry in data.get("low_watermark", [])],
        fencing_tokens={str(budget_id): int(token) for budget_id, token in data.get("fencing_tokens", {}).items()},
    )


def _completion_reserve_to_json(reserve: CompletionReserve) -> dict[str, object]:
    return {
        "reserve_id": reserve.reserve_id,
        "budget_id": reserve.budget_id,
        "purpose": reserve.purpose,
        "amounts": [_usage_amount_to_json(amount) for amount in reserve.amounts],
        "spendable_by": sorted(reserve.spendable_by),
        "expires_at": reserve.expires_at,
        "status": reserve.status,
        "reservation_id": reserve.reservation_id,
        "fencing_token": reserve.fencing_token,
    }


def _completion_reserve_from_json(data: dict[str, object]) -> CompletionReserve:
    return CompletionReserve(
        reserve_id=str(data["reserve_id"]),
        budget_id=str(data["budget_id"]),
        purpose=data["purpose"],
        amounts=[_usage_amount_from_json(entry) for entry in data.get("amounts", [])],
        spendable_by=frozenset(str(spender) for spender in data.get("spendable_by", [])),
        expires_at=data.get("expires_at"),
        status=data.get("status", "available"),
        reservation_id=data.get("reservation_id"),
        fencing_token=int(data.get("fencing_token", 0)),
    )


def _budget_ledger_to_snapshot(ledger: InMemoryBudgetLedger) -> dict[str, object]:
    return {
        "version": 1,
        "accounts": {
            budget_id: _account_to_json(account) for budget_id, account in sorted(ledger._accounts.items())
        },
        "allocated": {
            budget_id: _amount_map_to_json(amounts) for budget_id, amounts in sorted(ledger._allocated.items())
        },
        "reserved": {
            budget_id: _amount_map_to_json(amounts) for budget_id, amounts in sorted(ledger._reserved.items())
        },
        "committed": {
            budget_id: _amount_map_to_json(amounts) for budget_id, amounts in sorted(ledger._committed.items())
        },
        "overdraft": {
            budget_id: _amount_map_to_json(amounts) for budget_id, amounts in sorted(ledger._overdraft.items())
        },
        "reservations": {
            reservation_id: _reservation_to_json(reservation)
            for reservation_id, reservation in sorted(ledger._reservations.items())
        },
        "reservation_holds": {
            reservation_id: list(budget_ids)
            for reservation_id, budget_ids in sorted(ledger._reservation_holds.items())
        },
        "permits": {
            permit_id: _permit_to_json(permit) for permit_id, permit in sorted(ledger._permits.items())
        },
        "permit_spent": {
            permit_id: _amount_map_to_json(amounts) for permit_id, amounts in sorted(ledger._permit_spent.items())
        },
        "completion_reserves": {
            reserve_id: _completion_reserve_to_json(reserve)
            for reserve_id, reserve in sorted(ledger._completion_reserves.items())
        },
        "completion_reserve_holds": {
            reserve_id: list(budget_ids)
            for reserve_id, budget_ids in sorted(ledger._completion_reserve_holds.items())
        },
        "reservation_counter": ledger._reservation_counter,
        "fencing_counter": ledger._fencing_counter,
    }


def _budget_ledger_from_snapshot(snapshot: dict[str, object]) -> InMemoryBudgetLedger:
    ledger = InMemoryBudgetLedger()
    ledger._accounts = {
        str(budget_id): _account_from_json(account)
        for budget_id, account in snapshot.get("accounts", {}).items()
    }
    ledger._allocated = {
        str(budget_id): _amount_map_from_json(amounts)
        for budget_id, amounts in snapshot.get("allocated", {}).items()
    }
    ledger._reserved = {
        str(budget_id): _amount_map_from_json(amounts)
        for budget_id, amounts in snapshot.get("reserved", {}).items()
    }
    ledger._committed = {
        str(budget_id): _amount_map_from_json(amounts)
        for budget_id, amounts in snapshot.get("committed", {}).items()
    }
    ledger._overdraft = {
        str(budget_id): _amount_map_from_json(amounts)
        for budget_id, amounts in snapshot.get("overdraft", {}).items()
    }
    ledger._reservations = {
        str(reservation_id): _reservation_from_json(reservation)
        for reservation_id, reservation in snapshot.get("reservations", {}).items()
    }
    ledger._reservation_holds = {
        str(reservation_id): tuple(str(budget_id) for budget_id in budget_ids)
        for reservation_id, budget_ids in snapshot.get("reservation_holds", {}).items()
    }
    ledger._permits = {
        str(permit_id): _permit_from_json(permit) for permit_id, permit in snapshot.get("permits", {}).items()
    }
    ledger._permit_spent = {
        str(permit_id): _amount_map_from_json(amounts)
        for permit_id, amounts in snapshot.get("permit_spent", {}).items()
    }
    ledger._completion_reserves = {
        str(reserve_id): _completion_reserve_from_json(reserve)
        for reserve_id, reserve in snapshot.get("completion_reserves", {}).items()
    }
    ledger._completion_reserve_holds = {
        str(reserve_id): tuple(str(budget_id) for budget_id in budget_ids)
        for reserve_id, budget_ids in snapshot.get("completion_reserve_holds", {}).items()
    }
    ledger._reservation_counter = int(snapshot.get("reservation_counter", 0))
    ledger._fencing_counter = int(snapshot.get("fencing_counter", 0))
    return ledger


@dataclass(slots=True)
class SQLiteBudgetLedger:
    path: str | Path
    _connection: sqlite3.Connection = field(init=False, repr=False)
    _ledger: InMemoryBudgetLedger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._connection = sqlite3.connect(str(self.path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_snapshots (
              snapshot_id TEXT PRIMARY KEY,
              state_json TEXT NOT NULL
            )
            """
        )
        self._ledger = self._load_snapshot()

    @classmethod
    def in_memory(cls) -> SQLiteBudgetLedger:
        return cls(":memory:")

    @classmethod
    def open(cls, path: str | Path) -> SQLiteBudgetLedger:
        return cls(path)

    def close(self) -> None:
        self._connection.close()

    def allocate(
        self,
        budget_id: str,
        scope: ResourceRef,
        amounts: list[UsageAmount],
        *,
        policy_ref: str,
        parent_budget_id: str | None = None,
    ) -> BudgetAccount:
        return self._mutate(
            lambda ledger: ledger.allocate(
                budget_id,
                scope,
                amounts,
                policy_ref=policy_ref,
                parent_budget_id=parent_budget_id,
            )
        )

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
        return self._mutate(
            lambda ledger: ledger.reserve(
                budget_id,
                owner,
                amounts,
                purpose=purpose,
                expires_at=expires_at,
                reservation_id=reservation_id,
            )
        )

    def commit(
        self,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.commit(
                reservation_id,
                actual_amounts,
                max_overdraft=max_overdraft,
            )
        )

    def release(self, reservation_id: str) -> BudgetSettlement:
        return self._mutate(lambda ledger: ledger.release(reservation_id))

    def expire(self, reservation_id: str) -> BudgetSettlement:
        return self._mutate(lambda ledger: ledger.expire(reservation_id))

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
        return self._mutate(
            lambda ledger: ledger.issue_permit(
                permit_id,
                reservation_ids=reservation_ids,
                owner=owner,
                atomic_unit=atomic_unit,
                admission_epoch=admission_epoch,
                continuation_profile=continuation_profile,
                policy_snapshot_digest=policy_snapshot_digest,
                expires_at=expires_at,
                low_watermark=low_watermark,
            )
        )

    def commit_with_permit(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.commit_with_permit(
                permit_id,
                reservation_id,
                actual_amounts,
                max_overdraft=max_overdraft,
            )
        )

    def commit_with_permit_at(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        now: str,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.commit_with_permit_at(
                permit_id,
                reservation_id,
                actual_amounts,
                now=now,
                max_overdraft=max_overdraft,
            )
        )

    def release_with_permit(self, permit_id: str, reservation_id: str) -> BudgetSettlement:
        return self._mutate(lambda ledger: ledger.release_with_permit(permit_id, reservation_id))

    def release_with_permit_at(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        return self._mutate(lambda ledger: ledger.release_with_permit_at(permit_id, reservation_id, now=now))

    def create_completion_reserve(
        self,
        reserve_id: str,
        budget_id: str,
        *,
        purpose: CompletionReservePurpose,
        amounts: list[UsageAmount],
        spendable_by: tuple[str, ...],
        expires_at: str | None = None,
    ) -> CompletionReserve:
        return self._mutate(
            lambda ledger: ledger.create_completion_reserve(
                reserve_id,
                budget_id,
                purpose=purpose,
                amounts=amounts,
                spendable_by=spendable_by,
                expires_at=expires_at,
            )
        )

    def completion_reserve(self, reserve_id: str) -> CompletionReserve:
        self._ledger = self._load_snapshot()
        return self._ledger.completion_reserve(reserve_id)

    def spend_completion_reserve(
        self,
        reserve_id: str,
        spender: str,
        *,
        expires_at: str,
    ) -> BudgetReservation:
        return self._mutate(
            lambda ledger: ledger.spend_completion_reserve(
                reserve_id,
                spender,
                expires_at=expires_at,
            )
        )

    def release_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._mutate(lambda ledger: ledger.release_completion_reserve(reserve_id))

    def expire_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._mutate(lambda ledger: ledger.expire_completion_reserve(reserve_id))

    def balance(self, budget_id: str) -> BudgetBalance:
        self._ledger = self._load_snapshot()
        return self._ledger.balance(budget_id)

    def _load_snapshot(self) -> InMemoryBudgetLedger:
        row = self._connection.execute(
            "SELECT state_json FROM budget_ledger_snapshots WHERE snapshot_id = ?",
            ("default",),
        ).fetchone()
        if row is None:
            return InMemoryBudgetLedger()
        return _budget_ledger_from_snapshot(json.loads(row["state_json"]))

    def _save_snapshot(self, ledger: InMemoryBudgetLedger) -> None:
        self._connection.execute(
            """
            INSERT INTO budget_ledger_snapshots (snapshot_id, state_json)
            VALUES (?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET state_json = excluded.state_json
            """,
            (
                "default",
                json.dumps(
                    _budget_ledger_to_snapshot(ledger),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    def _mutate(self, action: Callable[[InMemoryBudgetLedger], BudgetLedgerResult]) -> BudgetLedgerResult:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._ledger = self._load_snapshot()
            result = action(self._ledger)
            self._save_snapshot(self._ledger)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            self._ledger = self._load_snapshot()
            raise
        return result
