from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias


LeaseTime: TypeAlias = int | float


class LeaseUnavailableError(RuntimeError):
    pass


class UnknownLeaseError(RuntimeError):
    pass


class StaleFencingTokenError(RuntimeError):
    pass


class InvalidLeaseRequestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActiveLease:
    resource: str
    owner: str
    units: int
    fencing_token: int
    attributes: MappingProxyType[str, object]
    acquired_at: LeaseTime
    expires_at: LeaseTime | None = None


@dataclass(slots=True)
class Lease:
    pool: InMemoryLeasePool
    lease_id: str
    resource: str
    owner: str
    units: int = 1
    fencing_token: int = 0

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
        return self.pool.release(self.lease_id)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


@dataclass(slots=True)
class InMemoryLeasePool:
    capacities: dict[str, int]
    active: dict[str, ActiveLease] = field(default_factory=dict)
    next_id: int = 1
    next_fencing_token: int = 1

    def __post_init__(self) -> None:
        capacities = dict(self.capacities)
        for resource, capacity in capacities.items():
            if not isinstance(resource, str) or not resource.strip():
                raise InvalidLeaseRequestError("lease resource name must be a non-empty string")
            if not isinstance(capacity, int) or capacity <= 0:
                raise InvalidLeaseRequestError(
                    f"lease capacity for {resource} must be a positive integer"
                )
        self.capacities = capacities

    def available(self, resource: str) -> int:
        capacity = self._capacity(resource)
        used = sum(active.units for active in self.active.values() if active.resource == resource)
        return capacity - used

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
        if not isinstance(owner, str) or not owner.strip():
            raise InvalidLeaseRequestError("lease owner must be a non-empty string")
        if not isinstance(units, int) or units <= 0:
            raise InvalidLeaseRequestError("lease units must be a positive integer")
        if expires_at is not None and expires_at <= acquired_at:
            raise InvalidLeaseRequestError("lease expires_at must be after acquisition")

        self.reap_expired(acquired_at)
        available = self.available(resource)
        if units > available:
            raise LeaseUnavailableError(
                f"no lease available for {resource}: requested {units}, "
                f"available {available}"
            )

        lease_id = f"lease-{self.next_id:06d}"
        self.next_id += 1
        fencing_token = self.next_fencing_token
        self.next_fencing_token += 1
        active = ActiveLease(
            resource=resource,
            owner=owner,
            units=units,
            fencing_token=fencing_token,
            attributes=MappingProxyType(dict(attributes or {})),
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

    def renew(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        expires_at: LeaseTime,
        renewed_at: LeaseTime = 0,
    ) -> int:
        if expires_at <= renewed_at:
            raise InvalidLeaseRequestError("lease expires_at must be after renewal")
        self.reap_expired(renewed_at)
        active = self._active_lease(lease_id)
        if active.fencing_token != fencing_token:
            raise StaleFencingTokenError(f"lease {lease_id} fencing token is stale")

        renewed_token = self.next_fencing_token
        self.next_fencing_token += 1
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

    def validate_fencing_token(self, lease_id: str, fencing_token: int) -> None:
        active = self._active_lease(lease_id)
        if active.fencing_token != fencing_token:
            raise StaleFencingTokenError(f"lease {lease_id} fencing token is stale")

    def attributes(self, lease_id: str) -> MappingProxyType[str, object]:
        return self._active_lease(lease_id).attributes

    def expires_at(self, lease_id: str) -> LeaseTime | None:
        return self._active_lease(lease_id).expires_at

    def reap_expired(self, now: LeaseTime) -> int:
        expired_ids = [
            lease_id
            for lease_id, active in self.active.items()
            if active.expires_at is not None and active.expires_at <= now
        ]
        for lease_id in expired_ids:
            self.active.pop(lease_id, None)
        return len(expired_ids)

    def release(self, lease_id: str) -> bool:
        return self.active.pop(lease_id, None) is not None

    def release_all(self, owner: str) -> None:
        for lease_id, active in list(self.active.items()):
            if active.owner == owner:
                self.release(lease_id)

    def _capacity(self, resource: str) -> int:
        if not isinstance(resource, str) or not resource.strip():
            raise InvalidLeaseRequestError("lease resource name must be a non-empty string")
        try:
            return self.capacities[resource]
        except KeyError as error:
            raise LeaseUnavailableError(f"unknown lease resource {resource}") from error

    def _active_lease(self, lease_id: str) -> ActiveLease:
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
