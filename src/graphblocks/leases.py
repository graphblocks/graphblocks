from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from types import MappingProxyType
from typing import ParamSpec, TypeAlias, TypeVar, cast


LeaseTime: TypeAlias = int | float
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _with_lease_pool_lock(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        pool = cast("InMemoryLeasePool", args[0])
        with pool._lock:
            return method(*args, **kwargs)

    return locked


class LeaseUnavailableError(RuntimeError):
    pass


class UnknownLeaseError(RuntimeError):
    pass


class StaleFencingTokenError(RuntimeError):
    pass


class InvalidLeaseRequestError(ValueError):
    pass


def _validate_non_empty_string(field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidLeaseRequestError(f"lease {field_name} must be a string")
    if not value.strip():
        raise InvalidLeaseRequestError(f"lease {field_name} must be a non-empty string")
    if value != value.strip():
        raise InvalidLeaseRequestError(
            f"lease {field_name} must not contain surrounding whitespace"
        )
    return value


def _validate_positive_integer(field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidLeaseRequestError(f"lease {field_name} must be a positive integer")
    if value <= 0:
        raise InvalidLeaseRequestError(f"lease {field_name} must be a positive integer")
    return value


def _validate_non_negative_integer(field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidLeaseRequestError(f"lease {field_name} must be a non-negative integer")
    if value < 0:
        raise InvalidLeaseRequestError(f"lease {field_name} must be a non-negative integer")
    return value


def _validate_time(field_name: str, value: object) -> LeaseTime:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidLeaseRequestError(f"lease {field_name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidLeaseRequestError(f"lease {field_name} must be finite")
    if value < 0:
        raise InvalidLeaseRequestError(f"lease {field_name} must be non-negative")
    return value


def _validate_optional_time(field_name: str, value: object | None) -> LeaseTime | None:
    if value is None:
        return None
    return _validate_time(field_name, value)


def _freeze_attributes(value: object) -> MappingProxyType[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidLeaseRequestError("lease attributes must be a mapping")
    attributes: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise InvalidLeaseRequestError("lease attribute keys must be strings")
        if not key.strip():
            raise InvalidLeaseRequestError("lease attribute keys must not be empty")
        if key != key.strip():
            raise InvalidLeaseRequestError(
                "lease attribute keys must not contain surrounding whitespace"
            )
        attributes[key] = _freeze_attribute_value(item)
    return MappingProxyType(attributes)


def _freeze_attribute_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidLeaseRequestError("lease attribute values must be JSON-compatible")
        return value
    if isinstance(value, Mapping):
        return _freeze_attributes(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_attribute_value(item) for item in value)
    raise InvalidLeaseRequestError("lease attribute values must be JSON-compatible")


@dataclass(frozen=True, slots=True)
class ActiveLease:
    resource: str
    owner: str
    units: int
    fencing_token: int
    attributes: MappingProxyType[str, object]
    acquired_at: LeaseTime
    expires_at: LeaseTime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource", _validate_non_empty_string("resource name", self.resource))
        object.__setattr__(self, "owner", _validate_non_empty_string("owner", self.owner))
        object.__setattr__(self, "units", _validate_positive_integer("units", self.units))
        object.__setattr__(
            self,
            "fencing_token",
            _validate_non_negative_integer("fencing_token", self.fencing_token),
        )
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))
        acquired_at = _validate_time("acquired_at", self.acquired_at)
        expires_at = _validate_optional_time("expires_at", self.expires_at)
        if expires_at is not None and expires_at <= acquired_at:
            raise InvalidLeaseRequestError("lease expires_at must be after acquisition")
        object.__setattr__(self, "acquired_at", acquired_at)
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(slots=True)
class Lease:
    pool: InMemoryLeasePool
    lease_id: str
    resource: str
    owner: str
    units: int = 1
    fencing_token: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.pool, InMemoryLeasePool):
            raise InvalidLeaseRequestError("lease pool must be an InMemoryLeasePool")
        self._validate_lease_id(self.lease_id)
        self.resource = _validate_non_empty_string("resource name", self.resource)
        self.owner = _validate_non_empty_string("owner", self.owner)
        self.units = _validate_positive_integer("units", self.units)
        self.fencing_token = _validate_non_negative_integer("fencing_token", self.fencing_token)

    @property
    def attributes(self) -> MappingProxyType[str, object]:
        return self.pool.attributes(self.lease_id)

    @property
    def expires_at(self) -> LeaseTime | None:
        return self.pool.expires_at(self.lease_id)

    def renew(self, *, expires_at: LeaseTime, renewed_at: LeaseTime = 0) -> int:
        self.fencing_token = self.pool.renew(
            self.lease_id,
            self.fencing_token,
            expires_at=expires_at,
            renewed_at=renewed_at,
        )
        return self.fencing_token

    def release(self) -> bool:
        return self.pool.release(self.lease_id, self.fencing_token)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    @staticmethod
    def _validate_lease_id(lease_id: object) -> str:
        return _validate_non_empty_string("lease_id", lease_id)


@dataclass(slots=True)
class InMemoryLeasePool:
    capacities: Mapping[str, int]
    active: dict[str, ActiveLease] = field(default_factory=dict)
    next_id: int = 1
    next_fencing_token: int = 1
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capacities, Mapping):
            raise InvalidLeaseRequestError("lease capacities must be a mapping")
        capacities = dict(self.capacities)
        for resource, capacity in capacities.items():
            _validate_non_empty_string("resource name", resource)
            if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
                raise InvalidLeaseRequestError(
                    f"lease capacity for {resource} must be a positive integer"
                )
        if not isinstance(self.active, Mapping):
            raise InvalidLeaseRequestError("lease active records must be a mapping")
        active_leases = dict(self.active)
        self.capacities = MappingProxyType(capacities)
        max_restored_id = 0
        max_restored_fencing_token = 0
        restored_units: dict[str, int] = {}
        restored_fencing_tokens: set[int] = set()
        for lease_id, active in active_leases.items():
            Lease._validate_lease_id(lease_id)
            if not isinstance(active, ActiveLease):
                raise InvalidLeaseRequestError("lease active records must be ActiveLease")
            if active.resource not in capacities:
                raise InvalidLeaseRequestError(
                    f"active lease {lease_id!r} references unknown resource {active.resource!r}"
                )
            restored_units[active.resource] = (
                restored_units.get(active.resource, 0) + active.units
            )
            if restored_units[active.resource] > capacities[active.resource]:
                raise InvalidLeaseRequestError(
                    f"active leases exceed capacity for resource {active.resource!r}"
                )
            if active.fencing_token <= 0:
                raise InvalidLeaseRequestError(
                    "active lease fencing tokens must be positive integers"
                )
            if active.fencing_token in restored_fencing_tokens:
                raise InvalidLeaseRequestError(
                    "active lease fencing tokens must be unique"
                )
            restored_fencing_tokens.add(active.fencing_token)
            suffix = lease_id.removeprefix("lease-")
            if (
                lease_id.startswith("lease-")
                and suffix.isascii()
                and suffix.isdecimal()
            ):
                max_restored_id = max(max_restored_id, int(suffix))
            max_restored_fencing_token = max(
                max_restored_fencing_token,
                active.fencing_token,
            )
        self.active = active_leases
        self.next_id = max(
            _validate_positive_integer("next_id", self.next_id),
            max_restored_id + 1,
        )
        self.next_fencing_token = max(
            _validate_positive_integer(
                "next_fencing_token",
                self.next_fencing_token,
            ),
            max_restored_fencing_token + 1,
        )

    @_with_lease_pool_lock
    def available(
        self,
        resource: str,
        *,
        now: LeaseTime | None = None,
    ) -> int:
        capacity = self._capacity(resource)
        if now is not None:
            self.reap_expired(now)
        used = sum(active.units for active in self.active.values() if active.resource == resource)
        return capacity - used

    @_with_lease_pool_lock
    def acquire(
        self,
        resource: str,
        owner: str,
        *,
        units: int = 1,
        attributes: dict[str, object] | None = None,
        expires_at: LeaseTime | None = None,
        acquired_at: LeaseTime = 0,
    ) -> Lease:
        self._capacity(resource)
        owner = _validate_non_empty_string("owner", owner)
        if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
            raise InvalidLeaseRequestError("lease units must be a positive integer")
        acquired_at = _validate_time("acquired_at", acquired_at)
        expires_at = _validate_optional_time("expires_at", expires_at)
        if expires_at is not None and expires_at <= acquired_at:
            raise InvalidLeaseRequestError("lease expires_at must be after acquisition")
        frozen_attributes = _freeze_attributes(
            {} if attributes is None else attributes
        )

        self.reap_expired(acquired_at)
        available = self.available(resource)
        if units > available:
            raise LeaseUnavailableError(
                f"no lease available for {resource}: requested {units}, "
                f"available {available}"
            )

        lease_id = self._allocate_lease_id()
        fencing_token = self._allocate_fencing_token()
        active = ActiveLease(
            resource=resource,
            owner=owner,
            units=units,
            fencing_token=fencing_token,
            attributes=frozen_attributes,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
        self.active[lease_id] = active
        return Lease(
            pool=self,
            lease_id=lease_id,
            resource=resource,
            owner=owner,
            units=units,
            fencing_token=fencing_token,
        )

    @_with_lease_pool_lock
    def renew(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        expires_at: LeaseTime,
        renewed_at: LeaseTime = 0,
    ) -> int:
        renewed_at = _validate_time("renewed_at", renewed_at)
        expires_at = _validate_time("expires_at", expires_at)
        if expires_at <= renewed_at:
            raise InvalidLeaseRequestError("lease expires_at must be after renewal")
        self.reap_expired(renewed_at)
        active = self._active_lease(lease_id)
        if renewed_at < active.acquired_at:
            raise InvalidLeaseRequestError(
                "lease renewed_at must not precede acquisition"
            )
        if active.fencing_token != fencing_token:
            raise StaleFencingTokenError(f"lease {lease_id} fencing token is stale")
        if active.expires_at is None or expires_at <= active.expires_at:
            raise InvalidLeaseRequestError(
                "lease renewal must extend the current expiration"
            )

        renewed_token = self._allocate_fencing_token()
        self.active[lease_id] = ActiveLease(
            resource=active.resource,
            owner=active.owner,
            units=active.units,
            fencing_token=renewed_token,
            attributes=active.attributes,
            acquired_at=active.acquired_at,
            expires_at=expires_at,
        )
        return renewed_token

    @_with_lease_pool_lock
    def validate_fencing_token(self, lease_id: str, fencing_token: int) -> None:
        Lease._validate_lease_id(lease_id)
        _validate_non_negative_integer("fencing_token", fencing_token)
        active = self._active_lease(lease_id)
        if active.fencing_token != fencing_token:
            raise StaleFencingTokenError(f"lease {lease_id} fencing token is stale")

    @_with_lease_pool_lock
    def attributes(self, lease_id: str) -> MappingProxyType[str, object]:
        return self._active_lease(lease_id).attributes

    @_with_lease_pool_lock
    def expires_at(self, lease_id: str) -> LeaseTime | None:
        return self._active_lease(lease_id).expires_at

    @_with_lease_pool_lock
    def reap_expired(self, now: LeaseTime) -> int:
        now = _validate_time("now", now)
        expired_ids = [
            lease_id
            for lease_id, active in self.active.items()
            if active.expires_at is not None and active.expires_at <= now
        ]
        for lease_id in expired_ids:
            self.active.pop(lease_id, None)
        return len(expired_ids)

    @_with_lease_pool_lock
    def release(
        self,
        lease_id: str,
        fencing_token: int | None = None,
    ) -> bool:
        Lease._validate_lease_id(lease_id)
        active = self.active.get(lease_id)
        if active is None:
            return False
        if fencing_token is None:
            raise InvalidLeaseRequestError(
                "lease fencing_token is required to release an active lease"
            )
        fencing_token = _validate_non_negative_integer(
            "fencing_token",
            fencing_token,
        )
        if active.fencing_token != fencing_token:
            raise StaleFencingTokenError(
                f"lease {lease_id} fencing token is stale"
            )
        self.active.pop(lease_id)
        return True

    @_with_lease_pool_lock
    def release_all(self, owner: str) -> None:
        owner = _validate_non_empty_string("owner", owner)
        for lease_id, active in list(self.active.items()):
            if active.owner == owner:
                self.active.pop(lease_id, None)

    def _capacity(self, resource: str) -> int:
        resource = _validate_non_empty_string("resource name", resource)
        try:
            return self.capacities[resource]
        except KeyError as error:
            raise LeaseUnavailableError(f"unknown lease resource {resource}") from error

    def _allocate_lease_id(self) -> str:
        lease_id = f"lease-{self.next_id:06d}"
        while lease_id in self.active:
            self.next_id += 1
            lease_id = f"lease-{self.next_id:06d}"
        self.next_id += 1
        return lease_id

    def _allocate_fencing_token(self) -> int:
        if self.active:
            self.next_fencing_token = max(
                self.next_fencing_token,
                max(active.fencing_token for active in self.active.values()) + 1,
            )
        fencing_token = self.next_fencing_token
        self.next_fencing_token += 1
        return fencing_token

    def _active_lease(self, lease_id: str) -> ActiveLease:
        Lease._validate_lease_id(lease_id)
        try:
            return self.active[lease_id]
        except KeyError as error:
            raise UnknownLeaseError(f"unknown lease {lease_id}") from error


__all__ = [
    "ActiveLease",
    "InMemoryLeasePool",
    "InvalidLeaseRequestError",
    "Lease",
    "LeaseTime",
    "LeaseUnavailableError",
    "StaleFencingTokenError",
    "UnknownLeaseError",
]
