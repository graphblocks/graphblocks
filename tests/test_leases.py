from __future__ import annotations

import pytest

from graphblocks.leases import InMemoryLeasePool, LeaseUnavailableError


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

