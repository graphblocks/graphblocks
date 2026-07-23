from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import math
from threading import Barrier, BrokenBarrierError

import pytest

from graphblocks.leases import (
    ActiveLease,
    InMemoryLeasePool,
    InvalidLeaseRequestError,
    Lease,
    LeaseUnavailableError,
    StaleFencingTokenError,
    UnknownLeaseError,
)


def test_lease_pool_rejects_acquire_past_capacity() -> None:
    pool = InMemoryLeasePool({"model": 1})
    lease = pool.acquire("model", owner="run-1")

    with pytest.raises(LeaseUnavailableError):
        pool.acquire("model", owner="run-2")

    lease.release()


def test_lease_pool_serializes_concurrent_acquisitions() -> None:
    pool = InMemoryLeasePool({"model": 1})
    writes = Barrier(2)

    class CoordinatedActive(dict[str, ActiveLease]):
        def __setitem__(self, key: str, value: ActiveLease) -> None:
            try:
                writes.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            super().__setitem__(key, value)

    pool.active = CoordinatedActive()

    def acquire(owner: str) -> str:
        try:
            pool.acquire("model", owner=owner)
        except LeaseUnavailableError:
            return "unavailable"
        return "acquired"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(acquire, ("run-1", "run-2")))

    assert sorted(outcomes) == ["acquired", "unavailable"]
    assert pool.available("model") == 0


def test_lease_release_is_idempotent() -> None:
    pool = InMemoryLeasePool({"model": 1})
    lease = pool.acquire("model", owner="run-1")

    lease.release()
    lease.release()

    assert pool.available("model") == 1


def test_lease_context_manager_releases_after_exception() -> None:
    pool = InMemoryLeasePool({"sandbox": 1})

    with pytest.raises(RuntimeError):
        with pool.acquire("sandbox", owner="run-1"):
            raise RuntimeError("failed work")

    assert pool.available("sandbox") == 1


def test_release_all_for_owner_cleans_owned_leases() -> None:
    pool = InMemoryLeasePool({"model": 2, "sandbox": 1})
    pool.acquire("model", owner="run-1")
    pool.acquire("sandbox", owner="run-1")
    pool.acquire("model", owner="run-2")

    pool.release_all(owner="run-1")

    assert pool.available("model") == 1
    assert pool.available("sandbox") == 1


def test_lease_pool_reserves_units_and_preserves_attributes() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 8})
    lease = pool.acquire(
        "licensed-tool",
        owner="run-1",
        units=5,
        attributes={"region": "us-east-1"},
    )

    assert pool.available("licensed-tool") == 3
    assert lease.units == 5
    assert lease.attributes["region"] == "us-east-1"
    with pytest.raises(LeaseUnavailableError, match="requested 4, available 3"):
        pool.acquire("licensed-tool", owner="run-2", units=4)

    assert lease.release() is True
    assert lease.release() is False
    assert pool.available("licensed-tool") == 8


def test_lease_attributes_are_deep_frozen_against_caller_mutation() -> None:
    pool = InMemoryLeasePool({"sandbox": 1})
    attributes = {
        "scope": {"tenant": "tenant-1", "labels": ["internal"]},
        "limits": [1, {"kind": "gpu"}],
    }

    lease = pool.acquire("sandbox", owner="run-1", attributes=attributes)
    attributes["scope"]["tenant"] = "mutated"
    attributes["scope"]["labels"].append("external")
    attributes["limits"][1]["kind"] = "cpu"

    assert lease.attributes["scope"]["tenant"] == "tenant-1"
    assert lease.attributes["scope"]["labels"] == ("internal",)
    assert lease.attributes["limits"] == (1, {"kind": "gpu"})
    with pytest.raises(TypeError):
        lease.attributes["scope"]["tenant"] = "mutated"
    with pytest.raises(TypeError):
        lease.attributes["limits"][1]["kind"] = "cpu"


def test_lease_pool_assigns_monotonic_fencing_tokens_and_validates_current_token() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    first = pool.acquire("licensed-tool", owner="worker")
    stale_token = first.fencing_token
    first.release()
    current = pool.acquire("licensed-tool", owner="worker")

    assert current.fencing_token > stale_token
    with pytest.raises(StaleFencingTokenError):
        pool.validate_fencing_token(current.lease_id, stale_token)

    pool.validate_fencing_token(current.lease_id, current.fencing_token)


def test_lease_pool_does_not_reuse_restored_identity_or_fencing_token() -> None:
    restored = ActiveLease(
        resource="model",
        owner="run-restored",
        units=1,
        fencing_token=5,
        attributes={},
        acquired_at=1,
    )
    restored_active = {"lease-000042": restored}
    pool = InMemoryLeasePool(
        {"model": 2},
        active=restored_active,
        next_id=1,
        next_fencing_token=5,
    )
    restored_active.clear()

    acquired = pool.acquire("model", owner="run-new", acquired_at=2)

    assert acquired.lease_id == "lease-000043"
    assert acquired.fencing_token > restored.fencing_token
    assert pool.active["lease-000042"] == restored
    assert len(pool.active) == 2


def test_lease_pool_uses_one_collision_proof_fencing_allocator_for_renew_and_acquire() -> None:
    restored = ActiveLease(
        resource="model",
        owner="run-restored",
        units=1,
        fencing_token=50,
        attributes={},
        acquired_at=1,
        expires_at=20,
    )
    pool = InMemoryLeasePool(
        {"model": 2},
        active={"lease-000010": restored},
        next_fencing_token=1,
    )

    renewed_token = pool.renew(
        "lease-000010",
        restored.fencing_token,
        expires_at=30,
        renewed_at=10,
    )
    acquired = pool.acquire("model", owner="run-new", acquired_at=10)

    assert renewed_token == 51
    assert acquired.fencing_token == 52
    assert len({lease.fencing_token for lease in pool.active.values()}) == 2


def test_lease_renewal_extends_expiration_and_rotates_fencing_token() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    lease = pool.acquire(
        "licensed-tool",
        owner="run-1",
        expires_at=15,
        acquired_at=10,
    )
    stale_token = lease.fencing_token

    renewed_token = lease.renew(expires_at=25, renewed_at=12)

    assert renewed_token > stale_token
    assert lease.fencing_token == renewed_token
    assert lease.expires_at == 25
    with pytest.raises(StaleFencingTokenError):
        pool.renew(lease.lease_id, stale_token, expires_at=30, renewed_at=13)


def test_stale_lease_handle_cannot_release_renewed_authority() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    current = pool.acquire(
        "licensed-tool",
        owner="run-1",
        expires_at=20,
        acquired_at=10,
    )
    stale = Lease(
        pool,
        current.lease_id,
        current.resource,
        current.owner,
        current.units,
        current.fencing_token,
    )
    current.renew(expires_at=30, renewed_at=11)

    with pytest.raises(StaleFencingTokenError):
        stale.release()
    with pytest.raises(
        InvalidLeaseRequestError,
        match="fencing_token is required",
    ):
        pool.release(current.lease_id)

    pool.validate_fencing_token(current.lease_id, current.fencing_token)
    assert current.release() is True


def test_lease_renewal_rejects_inactive_or_non_extending_authority() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 2})
    bounded = pool.acquire(
        "licensed-tool",
        owner="run-bounded",
        expires_at=20,
        acquired_at=10,
    )
    unbounded = pool.acquire(
        "licensed-tool",
        owner="run-unbounded",
        acquired_at=10,
    )

    for renewed_at, expires_at in ((9, 30), (11, 20), (11, 19)):
        with pytest.raises(InvalidLeaseRequestError):
            pool.renew(
                bounded.lease_id,
                bounded.fencing_token,
                expires_at=expires_at,
                renewed_at=renewed_at,
            )
    with pytest.raises(InvalidLeaseRequestError):
        pool.renew(
            unbounded.lease_id,
            unbounded.fencing_token,
            expires_at=30,
            renewed_at=11,
        )

    assert bounded.expires_at == 20
    assert bounded.fencing_token == pool.active[bounded.lease_id].fencing_token
    assert unbounded.expires_at is None
    assert unbounded.fencing_token == pool.active[unbounded.lease_id].fencing_token


def test_expired_leases_are_reaped_without_reusing_fencing_tokens() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    first = pool.acquire(
        "licensed-tool",
        owner="run-1",
        expires_at=15,
        acquired_at=10,
    )
    first_token = first.fencing_token

    assert pool.reap_expired(14) == 0
    assert pool.available("licensed-tool") == 0
    assert pool.reap_expired(15) == 1
    assert pool.available("licensed-tool") == 1
    with pytest.raises(UnknownLeaseError):
        pool.validate_fencing_token(first.lease_id, first_token)

    second = pool.acquire("licensed-tool", owner="run-2", acquired_at=16)
    assert second.fencing_token > first_token


def test_available_reaps_expired_leases_at_requested_time() -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    lease = pool.acquire(
        "licensed-tool",
        owner="run-1",
        expires_at=15,
        acquired_at=10,
    )

    assert pool.available("licensed-tool", now=14) == 0
    assert pool.available("licensed-tool", now=15) == 1
    with pytest.raises(UnknownLeaseError):
        pool.validate_fencing_token(lease.lease_id, lease.fencing_token)


def test_lease_pool_rejects_invalid_capacity_units_and_expiration() -> None:
    with pytest.raises(InvalidLeaseRequestError, match="positive integer"):
        InMemoryLeasePool({"bad": 0})
    with pytest.raises(InvalidLeaseRequestError, match="positive integer"):
        InMemoryLeasePool({"bad": True})  # type: ignore[dict-item]
    pool = InMemoryLeasePool({"licensed-tool": 1})

    with pytest.raises(InvalidLeaseRequestError, match="positive integer"):
        pool.acquire("licensed-tool", owner="run-1", units=0)
    with pytest.raises(InvalidLeaseRequestError, match="positive integer"):
        pool.acquire("licensed-tool", owner="run-1", units=True)  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="acquired_at must be a number"):
        pool.acquire("licensed-tool", owner="run-1", acquired_at="now")  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="expires_at must be a number"):
        pool.acquire("licensed-tool", owner="run-1", expires_at="later")  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="after acquisition"):
        pool.acquire("licensed-tool", owner="run-1", expires_at=10, acquired_at=10)
    with pytest.raises(InvalidLeaseRequestError, match="renewed_at must be a number"):
        pool.renew("missing", 1, expires_at=10, renewed_at="now")  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="after renewal"):
        pool.renew("missing", 1, expires_at=10, renewed_at=10)


def test_lease_pool_rejects_inconsistent_restored_active_state() -> None:
    active = ActiveLease(
        resource="model",
        owner="run-1",
        units=1,
        fencing_token=1,
        attributes={},
        acquired_at=1,
    )

    with pytest.raises(InvalidLeaseRequestError, match="references unknown resource"):
        InMemoryLeasePool({"sandbox": 1}, active={"lease-000001": active})
    with pytest.raises(InvalidLeaseRequestError, match="exceed capacity"):
        InMemoryLeasePool(
            {"model": 1},
            active={
                "lease-000001": active,
                "lease-000002": ActiveLease(
                    resource="model",
                    owner="run-2",
                    units=1,
                    fencing_token=2,
                    attributes={},
                    acquired_at=1,
                ),
            },
        )
    with pytest.raises(InvalidLeaseRequestError, match="fencing tokens must be positive"):
        InMemoryLeasePool(
            {"model": 1},
            active={
                "lease-000001": ActiveLease(
                    resource="model",
                    owner="run-1",
                    units=1,
                    fencing_token=0,
                    attributes={},
                    acquired_at=1,
                )
            },
        )
    with pytest.raises(InvalidLeaseRequestError, match="fencing tokens must be unique"):
        InMemoryLeasePool(
            {"model": 2},
            active={
                "lease-000001": active,
                "lease-000002": ActiveLease(
                    resource="model",
                    owner="run-2",
                    units=1,
                    fencing_token=1,
                    attributes={},
                    acquired_at=1,
                ),
            },
        )


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (
            lambda: InMemoryLeasePool({" model": 1}),
            "resource name must not contain surrounding whitespace",
        ),
        (
            lambda: ActiveLease("model", " run-1", 1, 1, {}, 1),
            "owner must not contain surrounding whitespace",
        ),
        (
            lambda: InMemoryLeasePool(
                {"model": 1},
                active={
                    " lease-000001": ActiveLease(
                        "model",
                        "run-1",
                        1,
                        1,
                        {},
                        1,
                    )
                },
            ),
            "lease_id must not contain surrounding whitespace",
        ),
    ),
)
def test_lease_records_reject_whitespace_wrapped_identities(factory, message: str) -> None:
    with pytest.raises(InvalidLeaseRequestError, match=message):
        factory()


@pytest.mark.parametrize("value", (math.nan, math.inf, -math.inf))
def test_lease_pool_rejects_non_finite_times(value: float) -> None:
    pool = InMemoryLeasePool({"licensed-tool": 1})
    lease = pool.acquire("licensed-tool", owner="run-1")

    with pytest.raises(InvalidLeaseRequestError, match="must be finite"):
        pool.acquire("licensed-tool", owner="run-2", acquired_at=value)
    with pytest.raises(InvalidLeaseRequestError, match="must be finite"):
        lease.renew(expires_at=value)
    with pytest.raises(InvalidLeaseRequestError, match="must be finite"):
        pool.reap_expired(value)


def test_lease_records_validate_identity_counters_times_and_attributes() -> None:
    with pytest.raises(InvalidLeaseRequestError, match="lease resource name must be a non-empty string"):
        ActiveLease(
            resource=" ",
            owner="run-1",
            units=1,
            fencing_token=1,
            attributes={},
            acquired_at=1,
        )
    with pytest.raises(InvalidLeaseRequestError, match="lease owner must be a string"):
        ActiveLease(
            resource="model",
            owner=object(),  # type: ignore[arg-type]
            units=1,
            fencing_token=1,
            attributes={},
            acquired_at=1,
        )
    with pytest.raises(InvalidLeaseRequestError, match="lease units must be a positive integer"):
        ActiveLease(
            resource="model",
            owner="run-1",
            units=True,  # type: ignore[arg-type]
            fencing_token=1,
            attributes={},
            acquired_at=1,
        )
    with pytest.raises(InvalidLeaseRequestError, match="lease fencing_token must be a non-negative integer"):
        ActiveLease(
            resource="model",
            owner="run-1",
            units=1,
            fencing_token=-1,
            attributes={},
            acquired_at=1,
        )
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must not be empty"):
        ActiveLease(
            resource="model",
            owner="run-1",
            units=1,
            fencing_token=1,
            attributes={" ": "acme"},
            acquired_at=1,
        )
    with pytest.raises(InvalidLeaseRequestError, match="lease expires_at must be after acquisition"):
        ActiveLease(
            resource="model",
            owner="run-1",
            units=1,
            fencing_token=1,
            attributes={},
            acquired_at=1,
            expires_at=1,
        )

    pool = InMemoryLeasePool({"model": 1})
    with pytest.raises(InvalidLeaseRequestError, match="lease pool must be an InMemoryLeasePool"):
        Lease(object(), "lease-1", "model", "run-1")  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="lease lease_id must be a non-empty string"):
        Lease(pool, " ", "model", "run-1")
    with pytest.raises(InvalidLeaseRequestError, match="lease next_id must be a positive integer"):
        InMemoryLeasePool({"model": 1}, next_id=0)
    with pytest.raises(InvalidLeaseRequestError, match="lease next_fencing_token must be a positive integer"):
        InMemoryLeasePool({"model": 1}, next_fencing_token=True)  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="lease active records must be ActiveLease"):
        InMemoryLeasePool({"model": 1}, active={"lease-1": object()})  # type: ignore[dict-item]


def test_lease_pool_rejects_invalid_attributes_and_release_inputs() -> None:
    pool = InMemoryLeasePool({"model": 1})

    with pytest.raises(InvalidLeaseRequestError, match="lease attributes must be a mapping"):
        pool.acquire("model", owner="run-1", attributes=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="lease attributes must be a mapping"):
        pool.acquire("model", owner="run-1", attributes=[])  # type: ignore[arg-type]
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must be strings"):
        pool.acquire("model", owner="run-1", attributes={object(): "value"})  # type: ignore[dict-item]
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must be strings"):
        pool.acquire("model", owner="run-1", attributes={"scope": {object(): "value"}})
    with pytest.raises(
        InvalidLeaseRequestError,
        match="attribute keys must not contain surrounding whitespace",
    ):
        pool.acquire("model", owner="run-1", attributes={" scope": "value"})
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute values must be JSON-compatible"):
        pool.acquire("model", owner="run-1", attributes={"scope": object()})
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute values must be JSON-compatible"):
        pool.acquire("model", owner="run-1", attributes={"scope": math.inf})
    with pytest.raises(InvalidLeaseRequestError, match="lease lease_id must be a non-empty string"):
        pool.release(" ")
    with pytest.raises(InvalidLeaseRequestError, match="lease owner must be a non-empty string"):
        pool.release_all(" ")
    with pytest.raises(InvalidLeaseRequestError, match="lease fencing_token must be a non-negative integer"):
        pool.validate_fencing_token("lease-1", True)  # type: ignore[arg-type]


def test_lease_pool_capacity_snapshot_is_read_only() -> None:
    capacities = {"model": 1}
    pool = InMemoryLeasePool(capacities)
    capacities["model"] = 10

    assert pool.available("model") == 1
    with pytest.raises(TypeError):
        pool.capacities["model"] = 10  # type: ignore[index]


def test_lease_pool_normalizes_hostile_mapping_failures() -> None:
    class ExplodingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("mapping changed during snapshot")

        def __iter__(self):
            raise RuntimeError("mapping changed during snapshot")

        def __len__(self) -> int:
            return 1

    class DuplicateMapping(dict[str, object]):
        def items(self):
            return (("model", 1), ("model", 2))

    with pytest.raises(InvalidLeaseRequestError, match="lease capacities could not be copied"):
        InMemoryLeasePool(ExplodingMapping())  # type: ignore[arg-type]
    pool = InMemoryLeasePool({"model": 1})
    with pytest.raises(InvalidLeaseRequestError, match="lease attributes could not be traversed"):
        pool.acquire("model", owner="run-1", attributes=ExplodingMapping())  # type: ignore[arg-type]
    with pytest.raises(
        InvalidLeaseRequestError,
        match="lease capacity resource names must be unique",
    ):
        InMemoryLeasePool(DuplicateMapping())  # type: ignore[arg-type]
    with pytest.raises(
        InvalidLeaseRequestError,
        match="lease attribute keys must be unique",
    ):
        pool.acquire(
            "model",
            owner="run-1",
            attributes=DuplicateMapping(),
        )


def test_lease_attributes_reject_cycles_overdepth_and_non_unicode_strings() -> None:
    pool = InMemoryLeasePool({"model": 1})
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(InvalidLeaseRequestError, match="lease attributes must not contain cyclic values"):
        pool.acquire("model", owner="run-1", attributes=cyclic)

    deeply_nested: dict[str, object] = {}
    cursor = deeply_nested
    for _ in range(66):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    with pytest.raises(InvalidLeaseRequestError, match="lease attributes exceed maximum JSON depth 64"):
        pool.acquire("model", owner="run-1", attributes=deeply_nested)
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must contain only Unicode scalar values"):
        pool.acquire("model", owner="run-1", attributes={"\ud800": "value"})
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute values must contain only Unicode scalar values"):
        pool.acquire("model", owner="run-1", attributes={"key": "\ud800"})
    with pytest.raises(InvalidLeaseRequestError, match="lease owner must contain only Unicode scalar values"):
        pool.acquire("model", owner="run-\ud800")
    for value in (-(1 << 63) - 1, 1 << 64):
        with pytest.raises(InvalidLeaseRequestError, match="lease attribute integer values must fit the JSON wire domain"):
            pool.acquire("model", owner="run-1", attributes={"value": value})


def test_lease_pool_rejects_values_outside_unsigned_wire_domain() -> None:
    maximum = (1 << 64) - 1
    with pytest.raises(InvalidLeaseRequestError, match=f"lease capacity for model must be at most {maximum}"):
        InMemoryLeasePool({"model": 1 << 64})
    with pytest.raises(InvalidLeaseRequestError, match=f"lease next_id must be at most {maximum}"):
        InMemoryLeasePool({"model": 1}, next_id=1 << 64)
    with pytest.raises(InvalidLeaseRequestError, match=f"lease fencing_token must be at most {maximum}"):
        ActiveLease("model", "run-1", 1, 1 << 64, {}, 1)


def test_lease_pool_fails_closed_without_mutation_when_counters_are_exhausted() -> None:
    maximum = (1 << 64) - 1
    id_exhausted = InMemoryLeasePool({"model": 1}, next_id=maximum)
    with pytest.raises(InvalidLeaseRequestError, match="lease identifier counter is exhausted"):
        id_exhausted.acquire("model", owner="run-1")
    assert (id_exhausted.next_id, id_exhausted.next_fencing_token, id_exhausted.active) == (
        maximum, 1, {},
    )

    fencing_exhausted = InMemoryLeasePool({"model": 1}, next_fencing_token=maximum)
    with pytest.raises(InvalidLeaseRequestError, match="lease fencing token counter is exhausted"):
        fencing_exhausted.acquire("model", owner="run-1")
    assert (fencing_exhausted.next_id, fencing_exhausted.next_fencing_token, fencing_exhausted.active) == (
        1, maximum, {},
    )


def test_lease_renewal_rejects_boolean_fencing_authority() -> None:
    pool = InMemoryLeasePool({"model": 1})
    lease = pool.acquire("model", owner="run-1", acquired_at=1, expires_at=2)
    with pytest.raises(InvalidLeaseRequestError, match="lease fencing_token must be a non-negative integer"):
        pool.renew(lease.lease_id, True, renewed_at=1, expires_at=3)  # type: ignore[arg-type]
    assert pool.active[lease.lease_id].fencing_token == lease.fencing_token
    assert pool.active[lease.lease_id].expires_at == 2
