from __future__ import annotations

from dataclasses import dataclass, field


class LeaseUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class Lease:
    pool: InMemoryLeasePool
    lease_id: str
    resource: str
    owner: str

    def release(self) -> None:
        self.pool.release(self.lease_id)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


@dataclass(slots=True)
class InMemoryLeasePool:
    capacities: dict[str, int]
    active: dict[str, tuple[str, str]] = field(default_factory=dict)
    next_id: int = 1

    def available(self, resource: str) -> int:
        used = sum(1 for active_resource, _owner in self.active.values() if active_resource == resource)
        return self.capacities[resource] - used

    def acquire(self, resource: str, owner: str) -> Lease:
        if self.available(resource) <= 0:
            raise LeaseUnavailableError(f"no lease available for {resource}")
        lease_id = f"lease-{self.next_id:06d}"
        self.next_id += 1
        self.active[lease_id] = (resource, owner)
        return Lease(self, lease_id, resource, owner)

    def release(self, lease_id: str) -> None:
        self.active.pop(lease_id, None)

    def release_all(self, owner: str) -> None:
        for lease_id, (_resource, active_owner) in list(self.active.items()):
            if active_owner == owner:
                self.release(lease_id)

