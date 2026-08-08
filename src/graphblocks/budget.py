from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
import sqlite3
from threading import RLock
from typing import TYPE_CHECKING, Literal, ParamSpec, TypeVar, cast, get_args

from ._lazy_exports import resolve_lazy_export as _resolve_lazy_export
from ._canonical_reference import canonical_dumps, canonical_loads
from .documents import FrozenDict
from .policy import ResourceRef

if TYPE_CHECKING:
    from .exhaustion import (
        AdmissionDecision,
        AfterUnitPolicy,
        ClientDelivery,
        ContinuationEnvelope,
        ContinuationWork,
        DurableResult,
        EffectPolicy,
        ExhaustionController,
        ExhaustionPolicy,
        ExhaustionPolicyError,
        ExhaustionPreset,
        ExhaustionUnit,
        ForbiddenWork,
        InFlightPolicy,
        MissingExhaustionBoundaryError,
        PartialOutputPolicy,
        WorkKind,
        validate_exhaustion_policy,
    )


AmountKey = tuple[str, str, tuple[tuple[str, str], ...]]
BudgetLedgerResult = TypeVar("BudgetLedgerResult")
BudgetLedgerParams = ParamSpec("BudgetLedgerParams")
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
_MAX_BUDGET_COUNTER = (1 << 64) - 1
_SQLITE_BUDGET_BUSY_TIMEOUT_MS = 5_000
_SQLITE_BUDGET_SCHEMA_VERSION = 1


class _FrozenUsageAmounts(tuple[object, ...]):
    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)


def _with_in_memory_budget_ledger_lock(
    method: Callable[BudgetLedgerParams, BudgetLedgerResult],
) -> Callable[BudgetLedgerParams, BudgetLedgerResult]:
    @wraps(method)
    def locked(
        *args: BudgetLedgerParams.args,
        **kwargs: BudgetLedgerParams.kwargs,
    ) -> BudgetLedgerResult:
        ledger = cast("InMemoryBudgetLedger", args[0])
        with ledger._lock:
            return method(*args, **kwargs)

    return locked


class BudgetError(RuntimeError):
    pass


class BudgetLedgerClosedError(BudgetError):
    pass


def _with_sqlite_budget_ledger_lock(
    method: Callable[BudgetLedgerParams, BudgetLedgerResult],
) -> Callable[BudgetLedgerParams, BudgetLedgerResult]:
    @wraps(method)
    def locked(
        *args: BudgetLedgerParams.args,
        **kwargs: BudgetLedgerParams.kwargs,
    ) -> BudgetLedgerResult:
        ledger = cast("SQLiteBudgetLedger", args[0])
        with ledger._lock:
            if ledger._closed:
                raise BudgetLedgerClosedError("SQLite budget ledger is closed")
            return method(*args, **kwargs)

    return locked


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


class BudgetAccountStateError(BudgetError):
    def __init__(self, budget_id: str, status: BudgetStatus) -> None:
        super().__init__(f"budget {budget_id!r} is {status}")
        self.budget_id = budget_id
        self.status = status


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


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        ) from error
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{owner} {field_name} must not contain control characters")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _parse_budget_permit_datetime(field_name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"budget permit {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"budget permit {field_name} must not be empty")
    normalized = value
    if normalized != normalized.strip() or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError(f"budget permit {field_name} must be an ISO datetime")
    timezone_start = 19
    if normalized[timezone_start] == ".":
        timezone_start += 1
        while timezone_start < len(normalized) and normalized[timezone_start].isdigit():
            timezone_start += 1
        if timezone_start == 20:
            raise ValueError(f"budget permit {field_name} must be an ISO datetime")
    suffix = normalized[timezone_start:]
    if suffix == "Z":
        normalized = f"{normalized[:timezone_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
    ):
        offset_hours = int(suffix[1:3])
        offset_minutes = int(suffix[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"budget permit {field_name} must be an ISO datetime")
    else:
        raise ValueError(f"budget permit {field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"budget permit {field_name} must be an ISO datetime") from error
    return parsed.astimezone(timezone.utc)


def _validate_non_negative_integer(owner: str, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    if value > _MAX_BUDGET_COUNTER:
        raise ValueError(
            f"{owner} {field_name} exceeds the supported integer range"
        )
    return value


def _validate_resource_ref(owner: str, field_name: str, value: object) -> ResourceRef:
    if not isinstance(value, ResourceRef):
        raise ValueError(f"{owner} {field_name} must be a ResourceRef")
    return value


def _validate_usage_amounts(owner: str, field_name: str, values: object) -> _FrozenUsageAmounts:
    if isinstance(values, str):
        raise ValueError(f"{owner} {field_name} must be a collection of UsageAmount")
    try:
        amounts = list(values)  # type: ignore[arg-type]
    except Exception as error:
        raise ValueError(f"{owner} {field_name} must be a collection of UsageAmount") from error
    if any(not isinstance(amount, UsageAmount) for amount in amounts):
        raise ValueError(f"{owner} {field_name} must contain UsageAmount records")
    return _FrozenUsageAmounts(amounts)


def _loads_strict_json(field_name: str, value: str) -> object:
    try:
        return canonical_loads(value)
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError(f"budget ledger {field_name} must be valid strict JSON") from error


def _dumps_strict_json(field_name: str, value: object) -> str:
    try:
        return canonical_dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"budget ledger {field_name} must be valid strict JSON") from error


def _validate_string_tuple(owner: str, field_name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except Exception as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in normalized:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return normalized


@dataclass(frozen=True, slots=True)
class UsageAmount:
    kind: str
    amount: Decimal
    unit: str
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("usage amount", "kind", self.kind)
        _validate_non_empty_string("usage amount", "unit", self.unit)
        if not isinstance(self.dimensions, Mapping):
            raise ValueError("usage amount dimensions must be a mapping")
        try:
            dimension_items = tuple(self.dimensions.items())
        except Exception as error:
            raise ValueError(
                "usage amount dimensions must be a readable mapping"
            ) from error
        dimensions: dict[str, str] = {}
        for key, value in dimension_items:
            if (
                not isinstance(key, str)
                or not key.strip()
                or key != key.strip()
                or not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(
                    "usage amount dimensions must be string keys and values"
                )
            if key in dimensions:
                raise ValueError(
                    f"usage amount dimensions contains duplicate key {key!r}"
                )
            dimensions[key] = value
        for key, value in dimensions.items():
            _validate_non_empty_string("usage amount", "dimension key", key)
            _validate_non_empty_string("usage amount", "dimension value", value)
        amount = self.amount
        if not isinstance(self.amount, Decimal):
            try:
                amount = Decimal(str(self.amount))
            except (InvalidOperation, ValueError) as error:
                raise ValueError("usage amount must be a decimal") from error
            object.__setattr__(self, "amount", amount)
        if not amount.is_finite():
            raise ValueError("usage amount must be finite")
        if amount < 0:
            raise ValueError("usage amount must be non-negative")
        object.__setattr__(self, "dimensions", FrozenDict(dimensions))


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
        _validate_non_empty_string("budget account", "budget_id", self.budget_id)
        _validate_resource_ref("budget account", "scope", self.scope)
        object.__setattr__(
            self,
            "allocated",
            _validate_usage_amounts("budget account", "allocated", self.allocated),
        )
        _validate_optional_non_empty_string("budget account", "parent_budget_id", self.parent_budget_id)
        if not isinstance(self.status, str) or self.status not in VALID_BUDGET_STATUSES:
            raise ValueError(f"unknown budget status {self.status!r}")
        if not isinstance(self.policy_ref, str):
            raise ValueError("budget account policy_ref must be a string")
        if self.policy_ref:
            _validate_non_empty_string(
                "budget account",
                "policy_ref",
                self.policy_ref,
            )
        _validate_non_negative_integer("budget account", "revision", self.revision)


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
        _validate_non_empty_string("budget reservation", "reservation_id", self.reservation_id)
        _validate_non_empty_string("budget reservation", "budget_id", self.budget_id)
        _validate_resource_ref("budget reservation", "owner", self.owner)
        object.__setattr__(
            self,
            "amounts",
            _validate_usage_amounts("budget reservation", "amounts", self.amounts),
        )
        if (
            not isinstance(self.purpose, str)
            or self.purpose not in VALID_RESERVATION_PURPOSES
        ):
            raise ValueError(f"unknown reservation purpose {self.purpose!r}")
        _validate_non_empty_string("budget reservation", "expires_at", self.expires_at)
        _validate_non_negative_integer("budget reservation", "fencing_token", self.fencing_token)
        if (
            not isinstance(self.status, str)
            or self.status not in VALID_RESERVATION_STATUSES
        ):
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

    def __post_init__(self) -> None:
        _validate_non_empty_string("budget balance", "budget_id", self.budget_id)
        for field_name in ("allocated", "reserved", "committed", "available", "overdraft"):
            object.__setattr__(
                self,
                field_name,
                _validate_usage_amounts("budget balance", field_name, getattr(self, field_name)),
            )
        _validate_non_negative_integer("budget balance", "revision", self.revision)
        if not isinstance(self.observed_at, str):
            raise ValueError("budget balance observed_at must be a string")


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
        _validate_non_empty_string("budget settlement", "reservation_id", self.reservation_id)
        _validate_non_empty_string("budget settlement", "budget_id", self.budget_id)
        for field_name in ("committed", "released", "overdraft"):
            object.__setattr__(
                self,
                field_name,
                _validate_usage_amounts("budget settlement", field_name, getattr(self, field_name)),
            )
        if (
            not isinstance(self.status, str)
            or self.status not in VALID_RESERVATION_STATUSES
        ):
            raise ValueError(f"unknown reservation status {self.status!r}")
        _validate_non_negative_integer("budget settlement", "revision", self.revision)


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
        _validate_non_empty_string("budget permit", "permit_id", self.permit_id)
        object.__setattr__(
            self,
            "reservation_refs",
            _validate_string_tuple("budget permit", "reservation_refs", self.reservation_refs),
        )
        if not self.reservation_refs:
            raise ValueError("budget permit reservation_refs must not be empty")
        if len(set(self.reservation_refs)) != len(self.reservation_refs):
            raise ValueError("budget permit reservation_refs must not contain duplicates")
        _validate_resource_ref("budget permit", "owner", self.owner)
        _validate_resource_ref("budget permit", "atomic_unit", self.atomic_unit)
        _validate_non_negative_integer("budget permit", "admission_epoch", self.admission_epoch)
        object.__setattr__(
            self,
            "authorized_amounts",
            _validate_usage_amounts("budget permit", "authorized_amounts", self.authorized_amounts),
        )
        _validate_non_empty_string(
            "budget permit",
            "continuation_profile",
            self.continuation_profile,
        )
        _validate_non_empty_string("budget permit", "policy_snapshot_digest", self.policy_snapshot_digest)
        _parse_budget_permit_datetime("expires_at", self.expires_at)
        object.__setattr__(
            self,
            "low_watermark",
            _validate_usage_amounts("budget permit", "low_watermark", self.low_watermark),
        )
        if not self.allows(self.low_watermark):
            raise ValueError(
                "budget permit low_watermark must not exceed authorized_amounts"
            )
        if not isinstance(self.fencing_tokens, Mapping):
            raise ValueError("budget permit fencing_tokens must be a mapping")
        try:
            fencing_token_items = tuple(self.fencing_tokens.items())
        except Exception as error:
            raise ValueError(
                "budget permit fencing_tokens must be a readable mapping"
            ) from error
        fencing_tokens: dict[str, int] = {}
        for reference, token in fencing_token_items:
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError(
                    "budget permit fencing token references must be non-empty strings"
                )
            if reference in fencing_tokens:
                raise ValueError(
                    f"budget permit fencing_tokens contains duplicate key {reference!r}"
                )
            fencing_tokens[reference] = token
        if not fencing_tokens:
            raise ValueError("budget permit fencing_tokens must not be empty")
        for reference, token in fencing_tokens.items():
            _validate_non_empty_string(
                "budget permit",
                "fencing token reference",
                reference,
            )
            if not isinstance(token, int) or isinstance(token, bool) or token <= 0:
                raise ValueError("budget permit fencing token values must be positive integers")
            if token > _MAX_BUDGET_COUNTER:
                raise ValueError(
                    "budget permit fencing token values exceed the supported range"
                )
        object.__setattr__(self, "fencing_tokens", FrozenDict(fencing_tokens))

    def allows(self, amounts: list[UsageAmount]) -> bool:
        authorized = _amounts_to_dict(self.authorized_amounts)
        requested = _amounts_to_dict(amounts)
        return all(amount <= authorized.get(key, Decimal("0")) for key, amount in requested.items())

    def is_active_at(self, now: str) -> bool:
        try:
            return _parse_budget_permit_datetime("expires_at", self.expires_at) > _parse_budget_permit_datetime(
                "now",
                now,
            )
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
        _validate_non_empty_string("completion reserve", "reserve_id", self.reserve_id)
        _validate_non_empty_string("completion reserve", "budget_id", self.budget_id)
        if (
            not isinstance(self.purpose, str)
            or self.purpose not in VALID_COMPLETION_RESERVE_PURPOSES
        ):
            raise ValueError(f"unknown completion reserve purpose {self.purpose!r}")
        object.__setattr__(
            self,
            "amounts",
            _validate_usage_amounts("completion reserve", "amounts", self.amounts),
        )
        object.__setattr__(
            self,
            "spendable_by",
            frozenset(_validate_string_tuple("completion reserve", "spendable_by", self.spendable_by)),
        )
        if not self.spendable_by:
            raise ValueError("completion reserve spendable_by must not be empty")
        _validate_optional_non_empty_string("completion reserve", "expires_at", self.expires_at)
        if (
            not isinstance(self.status, str)
            or self.status not in VALID_COMPLETION_RESERVE_STATUSES
        ):
            raise ValueError(f"unknown completion reserve status {self.status!r}")
        _validate_optional_non_empty_string("completion reserve", "reservation_id", self.reservation_id)
        _validate_non_negative_integer("completion reserve", "fencing_token", self.fencing_token)
        if self.status == "spent" and self.reservation_id is None:
            raise ValueError(
                "spent completion reserve requires reservation_id"
            )
        if self.status != "spent" and self.reservation_id is not None:
            raise ValueError(
                "unspent completion reserve must not define reservation_id"
            )


BudgetRecord = TypeVar("BudgetRecord", BudgetAccount, BudgetReservation, BudgetPermit, CompletionReserve)


def _copy_usage_amount(amount: UsageAmount) -> UsageAmount:
    return UsageAmount(
        kind=amount.kind,
        amount=amount.amount,
        unit=amount.unit,
        dimensions=dict(amount.dimensions),
    )


def _copy_usage_amounts(amounts: object) -> list[UsageAmount]:
    normalized = _validate_usage_amounts("budget", "amounts", amounts)
    return [_copy_usage_amount(amount) for amount in normalized]  # type: ignore[arg-type]


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


def _amounts_to_dict(amounts: object) -> dict[AmountKey, Decimal]:
    normalized = _validate_usage_amounts("budget", "amounts", amounts)
    values: dict[AmountKey, Decimal] = {}
    for amount in normalized:
        assert isinstance(amount, UsageAmount)
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
    _accounts: dict[str, BudgetAccount] = field(default_factory=dict, init=False)
    _allocated: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict, init=False)
    _reserved: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict, init=False)
    _committed: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict, init=False)
    _overdraft: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict, init=False)
    _reservations: dict[str, BudgetReservation] = field(default_factory=dict, init=False)
    _reservation_holds: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _permits: dict[str, BudgetPermit] = field(default_factory=dict, init=False)
    _permit_spent: dict[str, dict[AmountKey, Decimal]] = field(default_factory=dict, init=False)
    _completion_reserves: dict[str, CompletionReserve] = field(default_factory=dict, init=False)
    _completion_reserve_holds: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _reservation_counter: int = field(default=0, init=False)
    _fencing_counter: int = field(default=0, init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    @_with_in_memory_budget_ledger_lock
    def allocate(
        self,
        budget_id: str,
        scope: ResourceRef,
        amounts: list[UsageAmount],
        *,
        policy_ref: str,
        parent_budget_id: str | None = None,
    ) -> BudgetAccount:
        budget_id = _validate_non_empty_string(
            "budget account",
            "budget_id",
            budget_id,
        )
        _validate_resource_ref("budget account", "scope", scope)
        normalized_amounts = _validate_usage_amounts(
            "budget account",
            "allocated",
            amounts,
        )
        _validate_optional_non_empty_string(
            "budget account",
            "parent_budget_id",
            parent_budget_id,
        )
        if not isinstance(policy_ref, str):
            raise ValueError("budget account policy_ref must be a string")
        if policy_ref:
            _validate_non_empty_string(
                "budget account",
                "policy_ref",
                policy_ref,
            )
        if budget_id in self._accounts:
            raise BudgetConflictError(f"budget {budget_id!r} already exists")
        if parent_budget_id is not None and parent_budget_id not in self._accounts:
            raise BudgetNotFoundError(f"parent budget {parent_budget_id!r} does not exist")
        allocated = _amounts_to_dict(normalized_amounts)
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

    @_with_in_memory_budget_ledger_lock
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
        budget_id = _validate_non_empty_string(
            "budget reservation",
            "budget_id",
            budget_id,
        )
        _validate_resource_ref("budget reservation", "owner", owner)
        normalized_amounts = _validate_usage_amounts(
            "budget reservation",
            "amounts",
            amounts,
        )
        if (
            not isinstance(purpose, str)
            or purpose not in VALID_RESERVATION_PURPOSES
        ):
            raise ValueError(f"unknown reservation purpose {purpose!r}")
        expires_at = _validate_non_empty_string(
            "budget reservation",
            "expires_at",
            expires_at,
        )
        if reservation_id is not None:
            reservation_id = _validate_non_empty_string(
                "budget reservation",
                "reservation_id",
                reservation_id,
            )
        if budget_id not in self._accounts:
            raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
        requested = _amounts_to_dict(normalized_amounts)
        held_budget_ids = self._budget_chain(budget_id)
        self._ensure_accounts_active(held_budget_ids)
        for held_budget_id in held_budget_ids:
            available = _amounts_to_dict(self.balance(held_budget_id).available)
            for key, amount in requested.items():
                if amount > available.get(key, Decimal("0")):
                    raise BudgetExceededError(
                        f"budget {held_budget_id!r} has insufficient available {key[0]} {key[1]}"
                    )
        next_reservation_counter = _increment_budget_counter(
            "reservation",
            self._reservation_counter,
        )
        next_fencing_counter = _increment_budget_counter(
            "fencing",
            self._fencing_counter,
        )
        actual_reservation_id = (
            reservation_id
            or f"reservation-{next_reservation_counter:06d}"
        )
        if actual_reservation_id in self._reservations:
            raise BudgetConflictError(f"reservation {actual_reservation_id!r} already exists")
        numeric_suffix = _numeric_record_suffix(
            actual_reservation_id,
            "reservation-",
        )
        if numeric_suffix > _MAX_BUDGET_COUNTER:
            raise BudgetConflictError(
                "reservation id exceeds the supported counter range"
            )
        reservation = BudgetReservation(
            reservation_id=actual_reservation_id,
            budget_id=budget_id,
            owner=owner,
            amounts=_dict_to_amounts(requested),
            purpose=purpose,
            expires_at=expires_at,
            fencing_token=next_fencing_counter,
        )
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
        self._reservation_counter = max(
            next_reservation_counter,
            numeric_suffix,
        )
        self._fencing_counter = next_fencing_counter
        for held_budget_id in held_budget_ids:
            for key, amount in requested.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) + amount
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=next_revisions[held_budget_id],
            )
        self._reservations[actual_reservation_id] = _copy_budget_record(reservation)
        self._reservation_holds[actual_reservation_id] = held_budget_ids
        return _copy_budget_record(reservation)

    @_with_in_memory_budget_ledger_lock
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
        self._ensure_accounts_active(held_budget_ids)
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
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
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
                revision=next_revisions[held_budget_id],
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

    @_with_in_memory_budget_ledger_lock
    def release(self, reservation_id: str) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        held_budget_ids = self._reservation_holds.get(reservation_id, (budget_id,))
        reserved = _amounts_to_dict(reservation.amounts)
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
        for held_budget_id in held_budget_ids:
            for key, amount in reserved.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=next_revisions[held_budget_id],
            )
        self._reservations[reservation_id] = replace(reservation, status="released")
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            released=_dict_to_amounts(reserved),
            status="released",
            revision=self._accounts[budget_id].revision,
        )

    @_with_in_memory_budget_ledger_lock
    def expire(self, reservation_id: str) -> BudgetSettlement:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetReservationNotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation.status != "reserved":
            raise BudgetReservationStateError(f"reservation {reservation_id!r} is {reservation.status}")
        budget_id = reservation.budget_id
        held_budget_ids = self._reservation_holds.get(reservation_id, (budget_id,))
        reserved = _amounts_to_dict(reservation.amounts)
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
        for held_budget_id in held_budget_ids:
            for key, amount in reserved.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=next_revisions[held_budget_id],
            )
        self._reservations[reservation_id] = replace(reservation, status="expired")
        return BudgetSettlement(
            reservation_id=reservation_id,
            budget_id=budget_id,
            released=_dict_to_amounts(reserved),
            status="expired",
            revision=self._accounts[budget_id].revision,
        )

    @_with_in_memory_budget_ledger_lock
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
        permit_id = _validate_non_empty_string(
            "budget permit",
            "permit_id",
            permit_id,
        )
        normalized_reservation_ids = _validate_string_tuple(
            "budget permit",
            "reservation_refs",
            reservation_ids,
        )
        if not normalized_reservation_ids:
            raise ValueError("budget permit reservation_refs must not be empty")
        if len(set(normalized_reservation_ids)) != len(
            normalized_reservation_ids
        ):
            raise ValueError(
                "budget permit reservation_refs must not contain duplicates"
            )
        if permit_id in self._permits:
            raise BudgetConflictError(f"permit {permit_id!r} already exists")
        authorized: dict[AmountKey, Decimal] = {}
        fencing_tokens: dict[str, int] = {}
        for reservation_id in normalized_reservation_ids:
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
            reservation_refs=normalized_reservation_ids,
            owner=owner,
            atomic_unit=atomic_unit,
            admission_epoch=admission_epoch,
            authorized_amounts=_dict_to_amounts(authorized),
            low_watermark=() if low_watermark is None else low_watermark,  # type: ignore[arg-type]
            continuation_profile=continuation_profile,
            policy_snapshot_digest=policy_snapshot_digest,
            expires_at=expires_at,
            fencing_tokens=fencing_tokens,
        )
        self._permits[permit_id] = _copy_budget_record(permit)
        self._permit_spent[permit_id] = {}
        return _copy_budget_record(permit)

    @_with_in_memory_budget_ledger_lock
    def commit_with_permit(
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

    @_with_in_memory_budget_ledger_lock
    def commit_with_permit_at(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        now: str,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        return self.commit_with_permit(
            permit_id,
            reservation_id,
            actual_amounts,
            now=now,
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
        overdraft_limit = _amounts_to_dict(max_overdraft or [])
        self._ensure_permit_allows_additional(
            permit,
            actual,
            reservation_id,
            overdraft_limit,
        )
        settlement = self.commit(reservation_id, actual_amounts, max_overdraft=max_overdraft)
        spent = self._permit_spent.setdefault(permit.permit_id, {})
        for key, amount in actual.items():
            spent[key] = spent.get(key, Decimal("0")) + amount
            if spent[key] == 0:
                del spent[key]
        return settlement

    @_with_in_memory_budget_ledger_lock
    def release_with_permit(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        permit = self._permit_for_reservation(permit_id, reservation_id)
        self._ensure_permit_not_expired(permit, now)
        return self.release(reservation_id)

    @_with_in_memory_budget_ledger_lock
    def release_with_permit_at(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        return self.release_with_permit(permit_id, reservation_id, now=now)

    @_with_in_memory_budget_ledger_lock
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
        reserve_id = _validate_non_empty_string(
            "completion reserve",
            "reserve_id",
            reserve_id,
        )
        budget_id = _validate_non_empty_string(
            "completion reserve",
            "budget_id",
            budget_id,
        )
        if reserve_id in self._completion_reserves:
            raise BudgetCompletionReserveConflictError(f"completion reserve {reserve_id!r} already exists")
        if budget_id not in self._accounts:
            raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
        requested = _amounts_to_dict(amounts)
        held_budget_ids = self._budget_chain(budget_id)
        self._ensure_accounts_active(held_budget_ids)
        for held_budget_id in held_budget_ids:
            available = _amounts_to_dict(self.balance(held_budget_id).available)
            for key, amount in requested.items():
                if amount > available.get(key, Decimal("0")):
                    raise BudgetExceededError(
                        f"budget {held_budget_id!r} has insufficient available {key[0]} {key[1]}"
                    )
        next_fencing_counter = _increment_budget_counter(
            "fencing",
            self._fencing_counter,
        )
        reserve = CompletionReserve(
            reserve_id=reserve_id,
            budget_id=budget_id,
            purpose=purpose,
            amounts=_dict_to_amounts(requested),
            spendable_by=spendable_by,  # type: ignore[arg-type]
            expires_at=expires_at,
            fencing_token=next_fencing_counter,
        )
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
        self._fencing_counter = next_fencing_counter
        for held_budget_id in held_budget_ids:
            for key, amount in requested.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) + amount
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=next_revisions[held_budget_id],
            )
        self._completion_reserves[reserve_id] = _copy_budget_record(reserve)
        self._completion_reserve_holds[reserve_id] = held_budget_ids
        return _copy_budget_record(reserve)

    @_with_in_memory_budget_ledger_lock
    def completion_reserve(self, reserve_id: str) -> CompletionReserve:
        reserve = self._completion_reserves.get(reserve_id)
        if reserve is None:
            raise BudgetCompletionReserveNotFoundError(f"completion reserve {reserve_id!r} does not exist")
        return _copy_budget_record(reserve)

    @_with_in_memory_budget_ledger_lock
    def spend_completion_reserve(
        self,
        reserve_id: str,
        spender: str,
        *,
        expires_at: str,
    ) -> BudgetReservation:
        reserve_id = _validate_non_empty_string(
            "completion reserve",
            "reserve_id",
            reserve_id,
        )
        spender = _validate_non_empty_string(
            "completion reserve",
            "spender",
            spender,
        )
        expires_at = _validate_non_empty_string(
            "budget reservation",
            "expires_at",
            expires_at,
        )
        reserve = self._completion_reserves.get(reserve_id)
        if reserve is None:
            raise BudgetCompletionReserveNotFoundError(f"completion reserve {reserve_id!r} does not exist")
        if reserve.status != "available":
            raise BudgetCompletionReserveStateError(f"completion reserve {reserve_id!r} is {reserve.status}")
        held_budget_ids = self._completion_reserve_holds.get(reserve_id, (reserve.budget_id,))
        self._ensure_accounts_active(held_budget_ids)
        if spender not in reserve.spendable_by:
            raise BudgetCompletionReserveUnauthorizedError(reserve_id, spender)

        next_reservation_counter = _increment_budget_counter(
            "reservation",
            self._reservation_counter,
        )
        reservation_id = f"reservation-{next_reservation_counter:06d}"
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
        self._reservation_counter = next_reservation_counter
        self._reservations[reservation_id] = _copy_budget_record(reservation)
        self._reservation_holds[reservation_id] = held_budget_ids
        self._completion_reserves[reserve_id] = replace(
            reserve,
            status="spent",
            reservation_id=reservation_id,
        )
        return _copy_budget_record(reservation)

    @_with_in_memory_budget_ledger_lock
    def release_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._settle_completion_reserve(reserve_id, "released")

    @_with_in_memory_budget_ledger_lock
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
        held_budget_ids = self._completion_reserve_holds.get(
            reserve_id,
            (reserve.budget_id,),
        )
        next_revisions = {
            held_budget_id: _increment_budget_counter(
                "account revision",
                self._accounts[held_budget_id].revision,
            )
            for held_budget_id in held_budget_ids
        }
        for held_budget_id in held_budget_ids:
            for key, amount in amounts.items():
                self._reserved[held_budget_id][key] = self._reserved[held_budget_id].get(key, Decimal("0")) - amount
                if self._reserved[held_budget_id][key] == 0:
                    del self._reserved[held_budget_id][key]
            self._accounts[held_budget_id] = replace(
                self._accounts[held_budget_id],
                revision=next_revisions[held_budget_id],
            )
        updated = replace(reserve, status=status)
        self._completion_reserves[reserve_id] = updated
        return _copy_budget_record(updated)

    @_with_in_memory_budget_ledger_lock
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

    def _ensure_accounts_active(self, budget_ids: tuple[str, ...]) -> None:
        for budget_id in budget_ids:
            account = self._accounts.get(budget_id)
            if account is None:
                raise BudgetNotFoundError(f"budget {budget_id!r} does not exist")
            if account.status != "active":
                raise BudgetAccountStateError(budget_id, account.status)

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
        reservation_id: str,
        overdraft_limit: dict[AmountKey, Decimal],
    ) -> None:
        reservation = self._reservations[reservation_id]
        reservation_authorized = _amounts_to_dict(reservation.amounts)
        for key, amount in requested.items():
            if amount > (
                reservation_authorized.get(key, Decimal("0"))
                + overdraft_limit.get(key, Decimal("0"))
            ):
                raise BudgetExceededError(
                    f"permit {permit.permit_id!r} exceeds authorized {key[0]} {key[1]} "
                    f"for reservation {reservation_id!r}"
                )
        permit_authorized = _amounts_to_dict(permit.authorized_amounts)
        spent = self._permit_spent.get(permit.permit_id, {})
        for key, amount in requested.items():
            cumulative = spent.get(key, Decimal("0")) + amount
            cumulative_limit = permit_authorized.get(key, Decimal("0"))
            if cumulative > cumulative_limit:
                raise BudgetExceededError(
                    f"permit {permit.permit_id!r} exceeds cumulative authorized "
                    f"{key[0]} {key[1]}"
                )


def _usage_amount_to_json(amount: UsageAmount) -> dict[str, object]:
    return {
        "kind": amount.kind,
        "amount": str(amount.amount),
        "unit": amount.unit,
        "dimensions": dict(sorted(amount.dimensions.items())),
    }


def _usage_amount_from_json(data: dict[str, object]) -> UsageAmount:
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger usage amounts must be objects")
    raw_amount = data.get("amount")
    if not isinstance(raw_amount, str):
        raise ValueError("budget ledger usage amount values must be decimal strings")
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation as error:
        raise ValueError("budget ledger usage amount values must be decimal strings") from error
    return UsageAmount(
        kind=data.get("kind"),  # type: ignore[arg-type]
        amount=amount,
        unit=data.get("unit"),  # type: ignore[arg-type]
        dimensions=data.get("dimensions", {}),  # type: ignore[arg-type]
    )


def _resource_ref_to_json(resource: ResourceRef) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "tenant_id": resource.tenant_id,
        "attributes": dict(sorted(resource.attributes.items())),
    }


def _resource_ref_from_json(data: dict[str, object]) -> ResourceRef:
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger resource references must be objects")
    return ResourceRef(
        resource_id=data.get("resource_id"),  # type: ignore[arg-type]
        resource_kind=data.get("resource_kind"),
        tenant_id=data.get("tenant_id"),
        attributes=data.get("attributes", {}),  # type: ignore[arg-type]
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


def _amount_map_from_json(entries: object) -> dict[AmountKey, Decimal]:
    if not isinstance(entries, list):
        raise ValueError("budget ledger amount maps must be arrays")
    values: dict[AmountKey, Decimal] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("budget ledger amount map entries must be objects")
        kind = entry.get("kind")
        unit = entry.get("unit")
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or kind != kind.strip()
        ):
            raise ValueError("budget ledger amount map kinds must be non-empty strings")
        if (
            not isinstance(unit, str)
            or not unit.strip()
            or unit != unit.strip()
        ):
            raise ValueError("budget ledger amount map units must be non-empty strings")
        _validate_non_empty_string("budget ledger amount map", "kind", kind)
        _validate_non_empty_string("budget ledger amount map", "unit", unit)
        raw_dimensions = entry.get("dimensions", {})
        if not isinstance(raw_dimensions, Mapping):
            raise ValueError("budget ledger amount map dimensions must be objects")
        dimensions_map = dict(raw_dimensions)
        if any(
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
            or not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for key, value in dimensions_map.items()
        ):
            raise ValueError(
                "budget ledger amount map dimensions must have non-empty string keys and values"
            )
        dimensions = tuple(sorted(dimensions_map.items()))
        raw_amount = entry.get("amount")
        if not isinstance(raw_amount, str):
            raise ValueError(
                "budget ledger amount map values must be decimal strings"
            )
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as error:
            raise ValueError("budget ledger amount map values must be decimals") from error
        if not amount.is_finite():
            raise ValueError("budget ledger amount map values must be finite")
        if amount < 0:
            raise ValueError("budget ledger amount map values must be non-negative")
        key = (kind, unit, dimensions)
        if key in values:
            raise ValueError("budget ledger amount map keys must be unique")
        values[key] = amount
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
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger accounts must be objects")
    raw_allocated = data.get("allocated", [])
    if not isinstance(raw_allocated, list):
        raise ValueError("budget ledger account allocated values must be arrays")
    return BudgetAccount(
        budget_id=data.get("budget_id"),  # type: ignore[arg-type]
        scope=_resource_ref_from_json(data.get("scope")),  # type: ignore[arg-type]
        allocated=[_usage_amount_from_json(entry) for entry in raw_allocated],  # type: ignore[arg-type]
        parent_budget_id=data.get("parent_budget_id"),
        status=data.get("status", "active"),
        policy_ref=data.get("policy_ref", ""),  # type: ignore[arg-type]
        revision=data.get("revision", 0),  # type: ignore[arg-type]
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
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger reservations must be objects")
    raw_amounts = data.get("amounts", [])
    if not isinstance(raw_amounts, list):
        raise ValueError("budget ledger reservation amounts must be arrays")
    return BudgetReservation(
        reservation_id=data.get("reservation_id"),  # type: ignore[arg-type]
        budget_id=data.get("budget_id"),  # type: ignore[arg-type]
        owner=_resource_ref_from_json(data.get("owner")),  # type: ignore[arg-type]
        amounts=[_usage_amount_from_json(entry) for entry in raw_amounts],  # type: ignore[arg-type]
        purpose=data.get("purpose"),  # type: ignore[arg-type]
        expires_at=data.get("expires_at"),  # type: ignore[arg-type]
        fencing_token=data.get("fencing_token", 0),  # type: ignore[arg-type]
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
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger permits must be objects")
    reservation_refs = data.get("reservation_refs", [])
    authorized_amounts = data.get("authorized_amounts", [])
    low_watermark = data.get("low_watermark", [])
    fencing_tokens = data.get("fencing_tokens", {})
    if not isinstance(reservation_refs, list):
        raise ValueError("budget ledger permit reservation_refs must be arrays")
    if not isinstance(authorized_amounts, list):
        raise ValueError("budget ledger permit authorized_amounts must be arrays")
    if not isinstance(low_watermark, list):
        raise ValueError("budget ledger permit low_watermark must be arrays")
    if not isinstance(fencing_tokens, Mapping):
        raise ValueError("budget ledger permit fencing_tokens must be objects")
    return BudgetPermit(
        permit_id=data.get("permit_id"),  # type: ignore[arg-type]
        reservation_refs=tuple(reservation_refs),  # type: ignore[arg-type]
        owner=_resource_ref_from_json(data.get("owner")),  # type: ignore[arg-type]
        atomic_unit=_resource_ref_from_json(data.get("atomic_unit")),  # type: ignore[arg-type]
        admission_epoch=data.get("admission_epoch"),  # type: ignore[arg-type]
        authorized_amounts=[_usage_amount_from_json(entry) for entry in authorized_amounts],  # type: ignore[arg-type]
        continuation_profile=data.get("continuation_profile"),  # type: ignore[arg-type]
        policy_snapshot_digest=data.get("policy_snapshot_digest"),  # type: ignore[arg-type]
        expires_at=data.get("expires_at"),  # type: ignore[arg-type]
        low_watermark=[_usage_amount_from_json(entry) for entry in low_watermark],  # type: ignore[arg-type]
        fencing_tokens=dict(fencing_tokens),  # type: ignore[arg-type]
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
    if not isinstance(data, Mapping):
        raise ValueError("budget ledger completion reserves must be objects")
    raw_amounts = data.get("amounts", [])
    spendable_by = data.get("spendable_by", [])
    if not isinstance(raw_amounts, list):
        raise ValueError("budget ledger completion reserve amounts must be arrays")
    if not isinstance(spendable_by, list):
        raise ValueError("budget ledger completion reserve spendable_by must be arrays")
    return CompletionReserve(
        reserve_id=data.get("reserve_id"),  # type: ignore[arg-type]
        budget_id=data.get("budget_id"),  # type: ignore[arg-type]
        purpose=data.get("purpose"),  # type: ignore[arg-type]
        amounts=[_usage_amount_from_json(entry) for entry in raw_amounts],  # type: ignore[arg-type]
        spendable_by=frozenset(spendable_by),  # type: ignore[arg-type]
        expires_at=data.get("expires_at"),
        status=data.get("status", "available"),
        reservation_id=data.get("reservation_id"),
        fencing_token=data.get("fencing_token", 0),  # type: ignore[arg-type]
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


def _snapshot_object_map(
    snapshot: Mapping[str, object],
    field_name: str,
) -> dict[str, object]:
    value = snapshot.get(field_name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"budget ledger snapshot {field_name} must be an object")
    copied: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError(
                f"budget ledger snapshot {field_name} keys must be exact non-empty strings"
            )
        copied[key] = item
    return copied


def _snapshot_string_array_map(
    snapshot: Mapping[str, object],
    field_name: str,
) -> dict[str, tuple[str, ...]]:
    values = _snapshot_object_map(snapshot, field_name)
    normalized: dict[str, tuple[str, ...]] = {}
    for key, item in values.items():
        if not isinstance(item, list):
            raise ValueError(
                f"budget ledger snapshot {field_name} values must be arrays"
            )
        normalized[key] = _validate_string_tuple(
            "budget ledger snapshot",
            f"{field_name} values",
            item,
        )
    return normalized


def _snapshot_counter(snapshot: Mapping[str, object], field_name: str) -> int:
    value = snapshot.get(field_name, 0)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _MAX_BUDGET_COUNTER
    ):
        raise ValueError(
            f"budget ledger snapshot {field_name} must be a non-negative integer"
        )
    return value


def _validate_snapshot_record_ids(
    field_name: str,
    records: Mapping[str, object],
    record_id_field: str,
) -> None:
    for key, record in records.items():
        record_id = getattr(record, record_id_field)
        if key != record_id:
            raise ValueError(
                f"budget ledger snapshot {field_name} key must match {record_id_field}"
            )


def _snapshot_budget_chain(
    accounts: Mapping[str, BudgetAccount],
    budget_id: str,
) -> tuple[str, ...]:
    chain: list[str] = []
    seen: set[str] = set()
    current_id: str | None = budget_id
    while current_id is not None:
        if current_id in seen:
            raise ValueError(
                f"budget ledger snapshot budget hierarchy contains a cycle at {current_id!r}"
            )
        account = accounts.get(current_id)
        if account is None:
            raise ValueError(
                f"budget ledger snapshot references unknown budget {current_id!r}"
            )
        chain.append(current_id)
        seen.add(current_id)
        current_id = account.parent_budget_id
    return tuple(chain)


def _numeric_record_suffix(record_id: str, prefix: str) -> int:
    suffix = record_id.removeprefix(prefix)
    if record_id.startswith(prefix) and suffix.isascii() and suffix.isdecimal():
        return int(suffix)
    return 0


def _increment_budget_counter(field_name: str, value: int) -> int:
    if value >= _MAX_BUDGET_COUNTER:
        raise BudgetConflictError(
            f"budget ledger {field_name} counter is exhausted"
        )
    return value + 1


def _validate_budget_ledger_snapshot_consistency(
    ledger: InMemoryBudgetLedger,
) -> None:
    account_ids = set(ledger._accounts)
    for field_name, values in (
        ("allocated", ledger._allocated),
        ("reserved", ledger._reserved),
        ("committed", ledger._committed),
        ("overdraft", ledger._overdraft),
    ):
        if set(values) != account_ids:
            raise ValueError(
                f"budget ledger snapshot {field_name} keys must match account ids"
            )
    for budget_id, account in ledger._accounts.items():
        _snapshot_budget_chain(ledger._accounts, budget_id)
        if _amounts_to_dict(account.allocated) != ledger._allocated[budget_id]:
            raise ValueError(
                "budget ledger snapshot account allocated amounts must match allocated state"
            )

    if set(ledger._reservation_holds) != set(ledger._reservations):
        raise ValueError(
            "budget ledger snapshot reservation_holds keys must match reservation ids"
        )
    for reservation_id, reservation in ledger._reservations.items():
        expected_chain = _snapshot_budget_chain(
            ledger._accounts,
            reservation.budget_id,
        )
        if ledger._reservation_holds[reservation_id] != expected_chain:
            raise ValueError(
                "budget ledger snapshot reservation holds must match budget hierarchy"
            )

    if set(ledger._permit_spent) != set(ledger._permits):
        raise ValueError(
            "budget ledger snapshot permit_spent keys must match permit ids"
        )
    for permit in ledger._permits.values():
        if any(
            reservation_id not in ledger._reservations
            for reservation_id in permit.reservation_refs
        ):
            raise ValueError(
                "budget ledger snapshot permits must reference existing reservations"
            )
        referenced_reservations = tuple(
            ledger._reservations[reservation_id]
            for reservation_id in permit.reservation_refs
        )
        expected_authorized: dict[AmountKey, Decimal] = {}
        expected_fencing_tokens: dict[str, int] = {}
        for reservation in referenced_reservations:
            for key, amount in _amounts_to_dict(reservation.amounts).items():
                expected_authorized[key] = (
                    expected_authorized.get(key, Decimal("0")) + amount
                )
            for held_budget_id in ledger._reservation_holds[
                reservation.reservation_id
            ]:
                expected_fencing_tokens[held_budget_id] = max(
                    expected_fencing_tokens.get(held_budget_id, 0),
                    reservation.fencing_token,
                )
        if _amounts_to_dict(permit.authorized_amounts) != expected_authorized:
            raise ValueError(
                "budget ledger snapshot permit authorization must match its reservations"
            )
        if dict(permit.fencing_tokens) != expected_fencing_tokens:
            raise ValueError(
                "budget ledger snapshot permit fencing tokens must match its reservations"
            )
        spent = ledger._permit_spent[permit.permit_id]
        if any(
            amount > expected_authorized.get(key, Decimal("0"))
            for key, amount in spent.items()
        ):
            raise ValueError(
                "budget ledger snapshot permit spent amounts exceed authorization"
            )

    if set(ledger._completion_reserve_holds) != set(
        ledger._completion_reserves
    ):
        raise ValueError(
            "budget ledger snapshot completion_reserve_holds keys must match reserve ids"
        )
    for reserve_id, reserve in ledger._completion_reserves.items():
        expected_chain = _snapshot_budget_chain(ledger._accounts, reserve.budget_id)
        if ledger._completion_reserve_holds[reserve_id] != expected_chain:
            raise ValueError(
                "budget ledger snapshot completion reserve holds must match budget hierarchy"
            )
        if (
            reserve.reservation_id is not None
            and reserve.reservation_id not in ledger._reservations
        ):
            raise ValueError(
                "budget ledger snapshot completion reserve references unknown reservation"
            )
        if reserve.status == "spent":
            assert reserve.reservation_id is not None
            reservation = ledger._reservations[reserve.reservation_id]
            if (
                reservation.budget_id != reserve.budget_id
                or reservation.fencing_token != reserve.fencing_token
                or _amounts_to_dict(reservation.amounts)
                != _amounts_to_dict(reserve.amounts)
            ):
                raise ValueError(
                    "budget ledger snapshot spent completion reserve must match its reservation"
                )

    expected_reserved: dict[str, dict[AmountKey, Decimal]] = {
        budget_id: {} for budget_id in account_ids
    }
    for reservation in ledger._reservations.values():
        if reservation.status != "reserved":
            continue
        for held_budget_id in ledger._reservation_holds[
            reservation.reservation_id
        ]:
            for key, amount in _amounts_to_dict(reservation.amounts).items():
                expected_reserved[held_budget_id][key] = (
                    expected_reserved[held_budget_id].get(key, Decimal("0"))
                    + amount
                )
    for reserve in ledger._completion_reserves.values():
        if reserve.status != "available":
            continue
        for held_budget_id in ledger._completion_reserve_holds[
            reserve.reserve_id
        ]:
            for key, amount in _amounts_to_dict(reserve.amounts).items():
                expected_reserved[held_budget_id][key] = (
                    expected_reserved[held_budget_id].get(key, Decimal("0"))
                    + amount
                )
    if ledger._reserved != expected_reserved:
        raise ValueError(
            "budget ledger snapshot reserved amounts must match active holds"
        )

    max_reservation_counter = max(
        (
            _numeric_record_suffix(reservation_id, "reservation-")
            for reservation_id in ledger._reservations
        ),
        default=0,
    )
    if max_reservation_counter > _MAX_BUDGET_COUNTER:
        raise ValueError(
            "budget ledger snapshot reservation id exceeds the supported counter range"
        )
    ledger._reservation_counter = max(
        ledger._reservation_counter,
        max_reservation_counter,
    )
    max_fencing_token = max(
        (
            *(
                reservation.fencing_token
                for reservation in ledger._reservations.values()
            ),
            *(
                reserve.fencing_token
                for reserve in ledger._completion_reserves.values()
            ),
        ),
        default=0,
    )
    if ledger._fencing_counter < max_fencing_token:
        raise ValueError(
            "budget ledger snapshot fencing_counter precedes persisted fencing tokens"
        )


def _budget_ledger_from_snapshot(snapshot: dict[str, object]) -> InMemoryBudgetLedger:
    version = snapshot.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("budget ledger snapshot version must be integer 1")
    ledger = InMemoryBudgetLedger()
    raw_accounts = _snapshot_object_map(snapshot, "accounts")
    ledger._accounts = {
        budget_id: _account_from_json(account)  # type: ignore[arg-type]
        for budget_id, account in raw_accounts.items()
    }
    _validate_snapshot_record_ids("accounts", ledger._accounts, "budget_id")
    ledger._allocated = {
        budget_id: _amount_map_from_json(amounts)
        for budget_id, amounts in _snapshot_object_map(snapshot, "allocated").items()
    }
    ledger._reserved = {
        budget_id: _amount_map_from_json(amounts)
        for budget_id, amounts in _snapshot_object_map(snapshot, "reserved").items()
    }
    ledger._committed = {
        budget_id: _amount_map_from_json(amounts)
        for budget_id, amounts in _snapshot_object_map(snapshot, "committed").items()
    }
    ledger._overdraft = {
        budget_id: _amount_map_from_json(amounts)
        for budget_id, amounts in _snapshot_object_map(snapshot, "overdraft").items()
    }
    raw_reservations = _snapshot_object_map(snapshot, "reservations")
    ledger._reservations = {
        reservation_id: _reservation_from_json(reservation)  # type: ignore[arg-type]
        for reservation_id, reservation in raw_reservations.items()
    }
    _validate_snapshot_record_ids(
        "reservations",
        ledger._reservations,
        "reservation_id",
    )
    ledger._reservation_holds = _snapshot_string_array_map(
        snapshot,
        "reservation_holds",
    )
    raw_permits = _snapshot_object_map(snapshot, "permits")
    ledger._permits = {
        permit_id: _permit_from_json(permit)  # type: ignore[arg-type]
        for permit_id, permit in raw_permits.items()
    }
    _validate_snapshot_record_ids("permits", ledger._permits, "permit_id")
    ledger._permit_spent = {
        permit_id: _amount_map_from_json(amounts)
        for permit_id, amounts in _snapshot_object_map(snapshot, "permit_spent").items()
    }
    raw_completion_reserves = _snapshot_object_map(snapshot, "completion_reserves")
    ledger._completion_reserves = {
        reserve_id: _completion_reserve_from_json(reserve)  # type: ignore[arg-type]
        for reserve_id, reserve in raw_completion_reserves.items()
    }
    _validate_snapshot_record_ids(
        "completion_reserves",
        ledger._completion_reserves,
        "reserve_id",
    )
    ledger._completion_reserve_holds = _snapshot_string_array_map(
        snapshot,
        "completion_reserve_holds",
    )
    ledger._reservation_counter = _snapshot_counter(snapshot, "reservation_counter")
    ledger._fencing_counter = _snapshot_counter(snapshot, "fencing_counter")
    _validate_budget_ledger_snapshot_consistency(ledger)
    return ledger


@dataclass(slots=True)
class SQLiteBudgetLedger:
    path: str | Path
    _connection: sqlite3.Connection = field(init=False, repr=False)
    _ledger: InMemoryBudgetLedger = field(init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._connection = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            check_same_thread=False,
            timeout=_SQLITE_BUDGET_BUSY_TIMEOUT_MS / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            f"PRAGMA busy_timeout = {_SQLITE_BUDGET_BUSY_TIMEOUT_MS}"
        )
        if str(self.path) != ":memory:":
            try:
                self._connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold():
                    raise
        self._connection.create_function(
            "graphblocks_budget_snapshot",
            0,
            self._compatibility_snapshot_json,
        )
        try:
            self._initialize_storage()
            self._ledger, self._generation = self._load_normalized_state()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    @classmethod
    def in_memory(cls) -> SQLiteBudgetLedger:
        return cls(":memory:")

    @classmethod
    def open(cls, path: str | Path) -> SQLiteBudgetLedger:
        return cls(path)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, _result: self._persist_account_row(
                ledger,
                budget_id,
            ),
            mutation_kind="allocate",
        )

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, result: self._persist_reservation_state(
                ledger,
                result.reservation_id,
            ),
            mutation_kind="reserve",
        )

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
            ),
            mutation_kind="commit",
        )

    @_with_sqlite_budget_ledger_lock
    def release(self, reservation_id: str) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.release(reservation_id),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
            ),
            mutation_kind="release",
        )

    @_with_sqlite_budget_ledger_lock
    def expire(self, reservation_id: str) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.expire(reservation_id),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
            ),
            mutation_kind="expire",
        )

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, _result: self._persist_permit_row(
                ledger,
                permit_id,
            ),
            mutation_kind="issue_permit",
        )

    @_with_sqlite_budget_ledger_lock
    def commit_with_permit(
        self,
        permit_id: str,
        reservation_id: str,
        actual_amounts: list[UsageAmount],
        *,
        now: str,
        max_overdraft: list[UsageAmount] | None = None,
    ) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.commit_with_permit(
                permit_id,
                reservation_id,
                actual_amounts,
                now=now,
                max_overdraft=max_overdraft,
            ),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
                permit_id=permit_id,
            ),
            mutation_kind="commit_with_permit",
        )

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
                permit_id=permit_id,
            ),
            mutation_kind="commit_with_permit",
        )

    @_with_sqlite_budget_ledger_lock
    def release_with_permit(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.release_with_permit(
                permit_id,
                reservation_id,
                now=now,
            ),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
            ),
            mutation_kind="release_with_permit",
        )

    @_with_sqlite_budget_ledger_lock
    def release_with_permit_at(self, permit_id: str, reservation_id: str, *, now: str) -> BudgetSettlement:
        return self._mutate(
            lambda ledger: ledger.release_with_permit_at(
                permit_id,
                reservation_id,
                now=now,
            ),
            lambda ledger, _result: self._persist_reservation_state(
                ledger,
                reservation_id,
            ),
            mutation_kind="release_with_permit",
        )

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, _result: self._persist_completion_reserve_state(
                ledger,
                reserve_id,
            ),
            mutation_kind="create_completion_reserve",
        )

    @_with_sqlite_budget_ledger_lock
    def completion_reserve(self, reserve_id: str) -> CompletionReserve:
        self._refresh_for_read()
        return self._ledger.completion_reserve(reserve_id)

    @_with_sqlite_budget_ledger_lock
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
            ),
            lambda ledger, result: self._persist_spent_completion_reserve(
                ledger,
                reserve_id,
                result.reservation_id,
            ),
            mutation_kind="spend_completion_reserve",
        )

    @_with_sqlite_budget_ledger_lock
    def release_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._mutate(
            lambda ledger: ledger.release_completion_reserve(reserve_id),
            lambda ledger, _result: self._persist_completion_reserve_state(
                ledger,
                reserve_id,
            ),
            mutation_kind="release_completion_reserve",
        )

    @_with_sqlite_budget_ledger_lock
    def expire_completion_reserve(self, reserve_id: str) -> CompletionReserve:
        return self._mutate(
            lambda ledger: ledger.expire_completion_reserve(reserve_id),
            lambda ledger, _result: self._persist_completion_reserve_state(
                ledger,
                reserve_id,
            ),
            mutation_kind="expire_completion_reserve",
        )

    @_with_sqlite_budget_ledger_lock
    def balance(self, budget_id: str) -> BudgetBalance:
        self._refresh_for_read()
        return self._ledger.balance(budget_id)

    def _initialize_storage(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            legacy_snapshot_json: str | None = None
            snapshot_object = self._connection.execute(
                """
                SELECT type
                FROM sqlite_master
                WHERE name = 'budget_ledger_snapshots'
                """
            ).fetchone()
            if snapshot_object is not None:
                snapshot_object_type = snapshot_object["type"]
                if snapshot_object_type == "table":
                    row = self._connection.execute(
                        """
                        SELECT state_json
                        FROM budget_ledger_snapshots
                        WHERE snapshot_id = ?
                        """,
                        ("default",),
                    ).fetchone()
                    if row is not None:
                        legacy_snapshot_json = row["state_json"]
                    existing_legacy = self._connection.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE name = 'budget_ledger_legacy_snapshots'
                        """
                    ).fetchone()
                    if existing_legacy is not None:
                        raise ValueError(
                            "budget ledger contains conflicting legacy snapshot tables"
                        )
                    self._connection.execute(
                        """
                        ALTER TABLE budget_ledger_snapshots
                        RENAME TO budget_ledger_legacy_snapshots
                        """
                    )
                elif snapshot_object_type != "view":
                    raise ValueError(
                        "budget ledger snapshot compatibility object has an invalid type"
                    )

            self._create_normalized_tables()
            metadata = self._connection.execute(
                """
                SELECT schema_version, generation
                FROM budget_ledger_metadata
                WHERE singleton_id = 1
                """
            ).fetchone()
            if metadata is None:
                self._connection.execute(
                    """
                    INSERT INTO budget_ledger_metadata (
                      singleton_id,
                      schema_version,
                      generation,
                      reservation_counter,
                      fencing_counter
                    )
                    VALUES (1, ?, 0, 0, 0)
                    """,
                    (_SQLITE_BUDGET_SCHEMA_VERSION,),
                )
                if legacy_snapshot_json is not None:
                    legacy_ledger = self._decode_snapshot(
                        legacy_snapshot_json
                    )
                    self._replace_normalized_entity_rows(legacy_ledger)
                    self._update_metadata(
                        expected_generation=0,
                        next_generation=1,
                        ledger=legacy_ledger,
                    )
                    self._append_mutation(
                        1,
                        "legacy_snapshot_migrated",
                        {"snapshot_ids": ("default",)},
                    )
            else:
                self._validate_metadata_row(metadata)
                if legacy_snapshot_json is not None:
                    raise ValueError(
                        "budget ledger contains both normalized state and a legacy snapshot"
                    )

            self._apply_pending_snapshot_import()
            self._create_snapshot_compatibility_view()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _create_normalized_tables(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_metadata (
              singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
              schema_version INTEGER NOT NULL,
              generation INTEGER NOT NULL,
              reservation_counter INTEGER NOT NULL,
              fencing_counter INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_accounts (
              budget_id TEXT PRIMARY KEY,
              account_json TEXT NOT NULL,
              allocated_json TEXT NOT NULL,
              reserved_json TEXT NOT NULL,
              committed_json TEXT NOT NULL,
              overdraft_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_reservations (
              reservation_id TEXT PRIMARY KEY,
              reservation_json TEXT NOT NULL,
              holds_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_permits (
              permit_id TEXT PRIMARY KEY,
              permit_json TEXT NOT NULL,
              spent_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_completion_reserves (
              reserve_id TEXT PRIMARY KEY,
              reserve_json TEXT NOT NULL,
              holds_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_mutations (
              generation INTEGER PRIMARY KEY,
              mutation_kind TEXT NOT NULL,
              touched_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger_snapshot_imports (
              snapshot_id TEXT PRIMARY KEY,
              state_json TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            self._connection.execute(statement)

    def _create_snapshot_compatibility_view(self) -> None:
        self._connection.execute(
            """
            CREATE VIEW IF NOT EXISTS budget_ledger_snapshots AS
            SELECT
              'default' AS snapshot_id,
              graphblocks_budget_snapshot() AS state_json
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS budget_ledger_snapshot_update
            INSTEAD OF UPDATE OF state_json ON budget_ledger_snapshots
            BEGIN
              INSERT OR REPLACE INTO budget_ledger_snapshot_imports (
                snapshot_id,
                state_json
              )
              VALUES (NEW.snapshot_id, NEW.state_json);
            END
            """
        )

    def _compatibility_snapshot_json(self) -> str:
        ledger = getattr(self, "_ledger", None)
        if ledger is None:
            ledger = InMemoryBudgetLedger()
        return _dumps_strict_json(
            "state_json",
            _budget_ledger_to_snapshot(ledger),
        )

    @staticmethod
    def _decode_snapshot(state_json: object) -> InMemoryBudgetLedger:
        if not isinstance(state_json, str):
            raise ValueError("budget ledger state_json must be a string")
        snapshot = _loads_strict_json("state_json", state_json)
        if not isinstance(snapshot, dict):
            raise ValueError("budget ledger state_json must decode to an object")
        return _budget_ledger_from_snapshot(snapshot)

    @staticmethod
    def _load_json_field(field_name: str, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError(f"budget ledger {field_name} must be a string")
        return _loads_strict_json(field_name, value)

    @staticmethod
    def _validate_metadata_row(row: sqlite3.Row) -> tuple[int, int, int]:
        schema_version = row["schema_version"]
        generation = row["generation"]
        metadata_columns = frozenset(row.keys())
        reservation_counter = (
            row["reservation_counter"]
            if "reservation_counter" in metadata_columns
            else 0
        )
        fencing_counter = (
            row["fencing_counter"]
            if "fencing_counter" in metadata_columns
            else 0
        )
        if (
            not isinstance(schema_version, int)
            or schema_version != _SQLITE_BUDGET_SCHEMA_VERSION
        ):
            raise ValueError(
                "budget ledger normalized schema version is unsupported"
            )
        for field_name, value in (
            ("generation", generation),
            ("reservation_counter", reservation_counter),
            ("fencing_counter", fencing_counter),
        ):
            if (
                not isinstance(value, int)
                or not 0 <= value <= _MAX_BUDGET_COUNTER
            ):
                raise ValueError(
                    f"budget ledger normalized {field_name} must be a "
                    "non-negative integer"
                )
        return generation, reservation_counter, fencing_counter

    def _metadata_row(self) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT
              schema_version,
              generation,
              reservation_counter,
              fencing_counter
            FROM budget_ledger_metadata
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            raise ValueError("budget ledger normalized metadata is missing")
        return cast(sqlite3.Row, row)

    def _load_normalized_state(self) -> tuple[InMemoryBudgetLedger, int]:
        metadata = self._metadata_row()
        generation, reservation_counter, fencing_counter = (
            self._validate_metadata_row(metadata)
        )
        ledger = InMemoryBudgetLedger()
        for row in self._connection.execute(
            """
            SELECT
              budget_id,
              account_json,
              allocated_json,
              reserved_json,
              committed_json,
              overdraft_json
            FROM budget_ledger_accounts
            ORDER BY budget_id
            """
        ):
            budget_id = row["budget_id"]
            account_json = self._load_json_field(
                "account_json",
                row["account_json"],
            )
            if not isinstance(account_json, dict):
                raise ValueError(
                    "budget ledger account_json must decode to an object"
                )
            account = _account_from_json(account_json)
            if account.budget_id != budget_id:
                raise ValueError(
                    "budget ledger account row key must match budget_id"
                )
            ledger._accounts[budget_id] = account
            ledger._allocated[budget_id] = _amount_map_from_json(
                self._load_json_field(
                    "allocated_json",
                    row["allocated_json"],
                )
            )
            ledger._reserved[budget_id] = _amount_map_from_json(
                self._load_json_field(
                    "reserved_json",
                    row["reserved_json"],
                )
            )
            ledger._committed[budget_id] = _amount_map_from_json(
                self._load_json_field(
                    "committed_json",
                    row["committed_json"],
                )
            )
            ledger._overdraft[budget_id] = _amount_map_from_json(
                self._load_json_field(
                    "overdraft_json",
                    row["overdraft_json"],
                )
            )

        for row in self._connection.execute(
            """
            SELECT reservation_id, reservation_json, holds_json
            FROM budget_ledger_reservations
            ORDER BY reservation_id
            """
        ):
            reservation_id = row["reservation_id"]
            reservation_json = self._load_json_field(
                "reservation_json",
                row["reservation_json"],
            )
            if not isinstance(reservation_json, dict):
                raise ValueError(
                    "budget ledger reservation_json must decode to an object"
                )
            reservation = _reservation_from_json(reservation_json)
            if reservation.reservation_id != reservation_id:
                raise ValueError(
                    "budget ledger reservation row key must match reservation_id"
                )
            holds = self._load_json_field("holds_json", row["holds_json"])
            if not isinstance(holds, list):
                raise ValueError(
                    "budget ledger reservation holds_json must decode to an array"
                )
            ledger._reservations[reservation_id] = reservation
            ledger._reservation_holds[reservation_id] = (
                _validate_string_tuple(
                    "budget ledger reservation",
                    "holds",
                    holds,
                )
            )

        for row in self._connection.execute(
            """
            SELECT permit_id, permit_json, spent_json
            FROM budget_ledger_permits
            ORDER BY permit_id
            """
        ):
            permit_id = row["permit_id"]
            permit_json = self._load_json_field(
                "permit_json",
                row["permit_json"],
            )
            if not isinstance(permit_json, dict):
                raise ValueError(
                    "budget ledger permit_json must decode to an object"
                )
            permit = _permit_from_json(permit_json)
            if permit.permit_id != permit_id:
                raise ValueError(
                    "budget ledger permit row key must match permit_id"
                )
            ledger._permits[permit_id] = permit
            ledger._permit_spent[permit_id] = _amount_map_from_json(
                self._load_json_field(
                    "spent_json",
                    row["spent_json"],
                )
            )

        for row in self._connection.execute(
            """
            SELECT reserve_id, reserve_json, holds_json
            FROM budget_ledger_completion_reserves
            ORDER BY reserve_id
            """
        ):
            reserve_id = row["reserve_id"]
            reserve_json = self._load_json_field(
                "reserve_json",
                row["reserve_json"],
            )
            if not isinstance(reserve_json, dict):
                raise ValueError(
                    "budget ledger reserve_json must decode to an object"
                )
            reserve = _completion_reserve_from_json(reserve_json)
            if reserve.reserve_id != reserve_id:
                raise ValueError(
                    "budget ledger completion reserve row key must match reserve_id"
                )
            holds = self._load_json_field("holds_json", row["holds_json"])
            if not isinstance(holds, list):
                raise ValueError(
                    "budget ledger completion reserve holds_json must decode "
                    "to an array"
                )
            ledger._completion_reserves[reserve_id] = reserve
            ledger._completion_reserve_holds[reserve_id] = (
                _validate_string_tuple(
                    "budget ledger completion reserve",
                    "holds",
                    holds,
                )
            )

        ledger._reservation_counter = reservation_counter
        ledger._fencing_counter = fencing_counter
        _validate_budget_ledger_snapshot_consistency(ledger)
        return ledger, generation

    def _replace_normalized_entity_rows(
        self,
        ledger: InMemoryBudgetLedger,
    ) -> None:
        for table_name in (
            "budget_ledger_accounts",
            "budget_ledger_reservations",
            "budget_ledger_permits",
            "budget_ledger_completion_reserves",
        ):
            self._connection.execute(f"DELETE FROM {table_name}")
        for budget_id in sorted(ledger._accounts):
            self._persist_account_row(ledger, budget_id)
        for reservation_id in sorted(ledger._reservations):
            self._persist_reservation_row(ledger, reservation_id)
        for permit_id in sorted(ledger._permits):
            self._persist_permit_row(ledger, permit_id)
        for reserve_id in sorted(ledger._completion_reserves):
            self._persist_completion_reserve_row(ledger, reserve_id)

    def _persist_account_row(
        self,
        ledger: InMemoryBudgetLedger,
        budget_id: str,
    ) -> dict[str, tuple[str, ...]]:
        account = ledger._accounts[budget_id]
        self._connection.execute(
            """
            INSERT INTO budget_ledger_accounts (
              budget_id,
              account_json,
              allocated_json,
              reserved_json,
              committed_json,
              overdraft_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(budget_id) DO UPDATE SET
              account_json = excluded.account_json,
              allocated_json = excluded.allocated_json,
              reserved_json = excluded.reserved_json,
              committed_json = excluded.committed_json,
              overdraft_json = excluded.overdraft_json
            """,
            (
                budget_id,
                _dumps_strict_json(
                    "account_json",
                    _account_to_json(account),
                ),
                _dumps_strict_json(
                    "allocated_json",
                    _amount_map_to_json(ledger._allocated[budget_id]),
                ),
                _dumps_strict_json(
                    "reserved_json",
                    _amount_map_to_json(ledger._reserved[budget_id]),
                ),
                _dumps_strict_json(
                    "committed_json",
                    _amount_map_to_json(ledger._committed[budget_id]),
                ),
                _dumps_strict_json(
                    "overdraft_json",
                    _amount_map_to_json(ledger._overdraft[budget_id]),
                ),
            ),
        )
        return {"account_ids": (budget_id,)}

    def _persist_reservation_row(
        self,
        ledger: InMemoryBudgetLedger,
        reservation_id: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO budget_ledger_reservations (
              reservation_id,
              reservation_json,
              holds_json
            )
            VALUES (?, ?, ?)
            ON CONFLICT(reservation_id) DO UPDATE SET
              reservation_json = excluded.reservation_json,
              holds_json = excluded.holds_json
            """,
            (
                reservation_id,
                _dumps_strict_json(
                    "reservation_json",
                    _reservation_to_json(
                        ledger._reservations[reservation_id]
                    ),
                ),
                _dumps_strict_json(
                    "holds_json",
                    list(ledger._reservation_holds[reservation_id]),
                ),
            ),
        )

    def _persist_permit_row(
        self,
        ledger: InMemoryBudgetLedger,
        permit_id: str,
    ) -> dict[str, tuple[str, ...]]:
        self._connection.execute(
            """
            INSERT INTO budget_ledger_permits (
              permit_id,
              permit_json,
              spent_json
            )
            VALUES (?, ?, ?)
            ON CONFLICT(permit_id) DO UPDATE SET
              permit_json = excluded.permit_json,
              spent_json = excluded.spent_json
            """,
            (
                permit_id,
                _dumps_strict_json(
                    "permit_json",
                    _permit_to_json(ledger._permits[permit_id]),
                ),
                _dumps_strict_json(
                    "spent_json",
                    _amount_map_to_json(ledger._permit_spent[permit_id]),
                ),
            ),
        )
        return {"permit_ids": (permit_id,)}

    def _persist_completion_reserve_row(
        self,
        ledger: InMemoryBudgetLedger,
        reserve_id: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO budget_ledger_completion_reserves (
              reserve_id,
              reserve_json,
              holds_json
            )
            VALUES (?, ?, ?)
            ON CONFLICT(reserve_id) DO UPDATE SET
              reserve_json = excluded.reserve_json,
              holds_json = excluded.holds_json
            """,
            (
                reserve_id,
                _dumps_strict_json(
                    "reserve_json",
                    _completion_reserve_to_json(
                        ledger._completion_reserves[reserve_id]
                    ),
                ),
                _dumps_strict_json(
                    "holds_json",
                    list(ledger._completion_reserve_holds[reserve_id]),
                ),
            ),
        )

    def _persist_reservation_state(
        self,
        ledger: InMemoryBudgetLedger,
        reservation_id: str,
        *,
        permit_id: str | None = None,
    ) -> dict[str, tuple[str, ...]]:
        account_ids = ledger._reservation_holds[reservation_id]
        for budget_id in account_ids:
            self._persist_account_row(ledger, budget_id)
        self._persist_reservation_row(ledger, reservation_id)
        touched = {
            "account_ids": account_ids,
            "reservation_ids": (reservation_id,),
        }
        if permit_id is not None:
            self._persist_permit_row(ledger, permit_id)
            touched["permit_ids"] = (permit_id,)
        return touched

    def _persist_completion_reserve_state(
        self,
        ledger: InMemoryBudgetLedger,
        reserve_id: str,
    ) -> dict[str, tuple[str, ...]]:
        account_ids = ledger._completion_reserve_holds[reserve_id]
        for budget_id in account_ids:
            self._persist_account_row(ledger, budget_id)
        self._persist_completion_reserve_row(ledger, reserve_id)
        return {
            "account_ids": account_ids,
            "completion_reserve_ids": (reserve_id,),
        }

    def _persist_spent_completion_reserve(
        self,
        ledger: InMemoryBudgetLedger,
        reserve_id: str,
        reservation_id: str,
    ) -> dict[str, tuple[str, ...]]:
        self._persist_completion_reserve_row(ledger, reserve_id)
        self._persist_reservation_row(ledger, reservation_id)
        return {
            "completion_reserve_ids": (reserve_id,),
            "reservation_ids": (reservation_id,),
        }

    def _update_metadata(
        self,
        *,
        expected_generation: int,
        next_generation: int,
        ledger: InMemoryBudgetLedger,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE budget_ledger_metadata
            SET
              generation = ?,
              reservation_counter = ?,
              fencing_counter = ?
            WHERE singleton_id = 1
              AND generation = ?
            """,
            (
                next_generation,
                ledger._reservation_counter,
                ledger._fencing_counter,
                expected_generation,
            ),
        )
        if cursor.rowcount != 1:
            raise BudgetConflictError(
                "budget ledger generation changed during mutation"
            )

    def _append_mutation(
        self,
        generation: int,
        mutation_kind: str,
        touched: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO budget_ledger_mutations (
              generation,
              mutation_kind,
              touched_json
            )
            VALUES (?, ?, ?)
            """,
            (
                generation,
                mutation_kind,
                _dumps_strict_json(
                    "touched_json",
                    {
                        key: list(values)
                        for key, values in sorted(touched.items())
                    },
                ),
            ),
        )

    def _apply_pending_snapshot_import(self) -> bool:
        row = self._connection.execute(
            """
            SELECT state_json
            FROM budget_ledger_snapshot_imports
            WHERE snapshot_id = ?
            """,
            ("default",),
        ).fetchone()
        if row is None:
            return False
        ledger = self._decode_snapshot(row["state_json"])
        metadata = self._metadata_row()
        generation, _reservation_counter, _fencing_counter = (
            self._validate_metadata_row(metadata)
        )
        next_generation = _increment_budget_counter(
            "SQLite generation",
            generation,
        )
        self._replace_normalized_entity_rows(ledger)
        self._update_metadata(
            expected_generation=generation,
            next_generation=next_generation,
            ledger=ledger,
        )
        self._append_mutation(
            next_generation,
            "compatibility_snapshot_imported",
            {"snapshot_ids": ("default",)},
        )
        self._connection.execute(
            """
            DELETE FROM budget_ledger_snapshot_imports
            WHERE snapshot_id = ?
            """,
            ("default",),
        )
        return True

    def _refresh_in_transaction(self) -> None:
        imported = self._apply_pending_snapshot_import()
        metadata = self._metadata_row()
        generation, _reservation_counter, _fencing_counter = (
            self._validate_metadata_row(metadata)
        )
        if imported or generation != self._generation:
            self._ledger, self._generation = (
                self._load_normalized_state()
            )

    def _refresh_for_read(self) -> None:
        self._connection.execute("BEGIN")
        try:
            self._refresh_in_transaction()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _mutate(
        self,
        action: Callable[[InMemoryBudgetLedger], BudgetLedgerResult],
        persist: Callable[
            [InMemoryBudgetLedger, BudgetLedgerResult],
            Mapping[str, tuple[str, ...]],
        ],
        *,
        mutation_kind: str,
    ) -> BudgetLedgerResult:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._refresh_in_transaction()
            result = action(self._ledger)
            touched = persist(self._ledger, result)
            next_generation = _increment_budget_counter(
                "SQLite generation",
                self._generation,
            )
            self._update_metadata(
                expected_generation=self._generation,
                next_generation=next_generation,
                ledger=self._ledger,
            )
            self._append_mutation(
                next_generation,
                mutation_kind,
                touched,
            )
            self._connection.commit()
            self._generation = next_generation
        except Exception:
            self._connection.rollback()
            self._ledger, self._generation = (
                self._load_normalized_state()
            )
            raise
        return result


def admit_native_exhaustion_work(
    policy: dict[str, object],
    request: dict[str, object],
) -> dict[str, object]:
    from graphblocks_runtime import admit_exhaustion_work

    return admit_exhaustion_work(policy, request)


def evaluate_native_budget_ledger(operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_budget_ledger

    return evaluate_budget_ledger(operations)


_LAZY_EXPORT_MODULES = {
    "AdmissionDecision": ".exhaustion",
    "AfterUnitPolicy": ".exhaustion",
    "ClientDelivery": ".exhaustion",
    "ContinuationEnvelope": ".exhaustion",
    "ContinuationWork": ".exhaustion",
    "DurableResult": ".exhaustion",
    "EffectPolicy": ".exhaustion",
    "ExhaustionController": ".exhaustion",
    "ExhaustionPolicy": ".exhaustion",
    "ExhaustionPolicyError": ".exhaustion",
    "ExhaustionPreset": ".exhaustion",
    "ExhaustionUnit": ".exhaustion",
    "ForbiddenWork": ".exhaustion",
    "InFlightPolicy": ".exhaustion",
    "MissingExhaustionBoundaryError": ".exhaustion",
    "PartialOutputPolicy": ".exhaustion",
    "WorkKind": ".exhaustion",
    "validate_exhaustion_policy": ".exhaustion",
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        return _resolve_lazy_export(
            module_name=__name__,
            package_name=__package__,
            namespace=globals(),
            export_modules=_LAZY_EXPORT_MODULES,
            name=name,
        )

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(_LAZY_EXPORT_MODULES))


__all__ = [
    "AdmissionDecision",
    "AfterUnitPolicy",
    "BudgetAccount",
    "BudgetAccountStateError",
    "BudgetBalance",
    "BudgetCompletionReserveConflictError",
    "BudgetCompletionReserveNotFoundError",
    "BudgetCompletionReserveStateError",
    "BudgetCompletionReserveUnauthorizedError",
    "BudgetConflictError",
    "BudgetError",
    "BudgetExceededError",
    "BudgetLedgerClosedError",
    "BudgetNotFoundError",
    "BudgetPermit",
    "BudgetPermitExpiredError",
    "BudgetPermitFencingError",
    "BudgetPermitNotFoundError",
    "BudgetPermitScopeError",
    "BudgetReservation",
    "BudgetReservationNotFoundError",
    "BudgetReservationStateError",
    "BudgetSettlement",
    "BudgetStatus",
    "ClientDelivery",
    "CompletionReserve",
    "CompletionReservePurpose",
    "CompletionReserveStatus",
    "ContinuationEnvelope",
    "ContinuationWork",
    "DurableResult",
    "EffectPolicy",
    "ExhaustionController",
    "ExhaustionPolicy",
    "ExhaustionPolicyError",
    "ExhaustionPreset",
    "ExhaustionUnit",
    "ForbiddenWork",
    "InMemoryBudgetLedger",
    "InFlightPolicy",
    "MissingExhaustionBoundaryError",
    "PartialOutputPolicy",
    "ReservationPurpose",
    "ReservationStatus",
    "ResourceRef",
    "SQLiteBudgetLedger",
    "UsageAmount",
    "VALID_BUDGET_STATUSES",
    "VALID_COMPLETION_RESERVE_PURPOSES",
    "VALID_COMPLETION_RESERVE_STATUSES",
    "VALID_RESERVATION_PURPOSES",
    "VALID_RESERVATION_STATUSES",
    "WorkKind",
    "admit_native_exhaustion_work",
    "evaluate_native_budget_ledger",
    "validate_exhaustion_policy",
]
