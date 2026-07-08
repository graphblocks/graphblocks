from __future__ import annotations

import math

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
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must be strings"):
        pool.acquire("model", owner="run-1", attributes={object(): "value"})  # type: ignore[dict-item]
    with pytest.raises(InvalidLeaseRequestError, match="lease attribute keys must be strings"):
        pool.acquire("model", owner="run-1", attributes={"scope": {object(): "value"}})
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
