from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from graphblocks.clocks import (
    AuditWallClock,
    AuthorityWallClock,
    ClockSkewError,
    ClockValueError,
    MonotonicDeadline,
    system_audit_wall_timestamp,
    system_authority_wall_milliseconds,
)
from graphblocks.leases import InMemoryLeasePool
from graphblocks.server import GraphBlocksServerApp


class ScriptedClock:
    def __init__(self, *values: object) -> None:
        self._values = iter(values)

    def __call__(self) -> object:
        return next(self._values)


def test_monotonic_deadline_uses_only_its_process_clock() -> None:
    clock = ScriptedClock(10.0, 12.0, 16.0)
    deadline = MonotonicDeadline.after(5.0, clock=clock)  # type: ignore[arg-type]

    assert deadline.remaining_seconds() == 3.0
    assert deadline.remaining_seconds() == 0.0
    assert MonotonicDeadline.after(None, clock=clock).remaining_seconds() is None  # type: ignore[arg-type]


@pytest.mark.parametrize("value", (True, -1, float("nan"), float("inf")))
def test_monotonic_deadline_rejects_invalid_clock_values(value: object) -> None:
    with pytest.raises(ClockValueError, match="monotonic clock"):
        MonotonicDeadline.after(
            1,
            clock=lambda: value,  # type: ignore[return-value]
        )


def test_monotonic_deadline_rejects_invalid_configuration() -> None:
    with pytest.raises(ClockValueError, match="must be callable"):
        MonotonicDeadline.after(1, clock=object())  # type: ignore[arg-type]
    with pytest.raises(ClockValueError, match="must be callable"):
        MonotonicDeadline(1, clock=object())  # type: ignore[arg-type]
    with pytest.raises(ClockValueError, match="finite number"):
        MonotonicDeadline.after(
            10**10_000,
            clock=lambda: 0,
        )
    with pytest.raises(ClockValueError, match="deadline expiration"):
        MonotonicDeadline(-1)


def test_authority_wall_clock_accepts_elapsed_time_and_clamps_small_rollback() -> None:
    wall = ScriptedClock(1_000, 950, 1_100)
    process = ScriptedClock(1.0, 1.0, 1.1)
    clock = AuthorityWallClock(
        wall_milliseconds=wall,  # type: ignore[arg-type]
        monotonic_seconds=process,  # type: ignore[arg-type]
        max_skew_milliseconds=100,
    )

    assert clock.now_milliseconds() == 1_000
    assert clock.now_milliseconds() == 1_000
    assert clock.now_milliseconds() == 1_100


@pytest.mark.parametrize("jumped_wall", (800, 1_200))
def test_authority_wall_clock_rejects_rollback_and_forward_skew(
    jumped_wall: int,
) -> None:
    clock = AuthorityWallClock(
        wall_milliseconds=ScriptedClock(1_000, jumped_wall),  # type: ignore[arg-type]
        monotonic_seconds=ScriptedClock(1.0, 1.0),  # type: ignore[arg-type]
        max_skew_milliseconds=100,
    )

    assert clock.now_milliseconds() == 1_000
    with pytest.raises(ClockSkewError, match="skew policy"):
        clock.now_milliseconds()


def test_authority_wall_clock_rejects_monotonic_rollback() -> None:
    clock = AuthorityWallClock(
        wall_milliseconds=ScriptedClock(1_000, 1_001),  # type: ignore[arg-type]
        monotonic_seconds=ScriptedClock(2.0, 1.0),  # type: ignore[arg-type]
    )

    assert clock.now_milliseconds() == 1_000
    with pytest.raises(ClockSkewError, match="monotonic clock moved backwards"):
        clock.now_milliseconds()


@pytest.mark.parametrize("value", (True, 1.5, -1))
def test_authority_wall_clock_rejects_invalid_wall_values(value: object) -> None:
    clock = AuthorityWallClock(
        wall_milliseconds=lambda: value,  # type: ignore[return-value]
        monotonic_seconds=lambda: 0,
    )

    with pytest.raises(ClockValueError, match="authority wall clock"):
        clock.now_milliseconds()


@pytest.mark.parametrize(
    ("field", "kwargs"),
    (
        ("wall clock", {"wall_milliseconds": object()}),
        ("monotonic clock", {"monotonic_seconds": object()}),
        ("max_skew", {"max_skew_milliseconds": True}),
        ("max_skew", {"max_skew_milliseconds": -1}),
    ),
)
def test_authority_wall_clock_rejects_invalid_configuration(
    field: str,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ClockValueError, match=field):
        AuthorityWallClock(**kwargs)  # type: ignore[arg-type]


def test_lease_pool_uses_authority_clock_for_expiry_without_wall_time_takeover() -> (
    None
):
    current = {"wall": 1_000, "monotonic": 1.0}
    authority = AuthorityWallClock(
        wall_milliseconds=lambda: current["wall"],
        monotonic_seconds=lambda: current["monotonic"],
        max_skew_milliseconds=10,
    )
    pool = InMemoryLeasePool(
        {"worker": 1},
        authority_clock=authority,
    )
    lease = pool.acquire("worker", owner="run-1", expires_at=1_100)

    current.update(wall=1_050, monotonic=1.05)
    assert pool.available("worker") == 0

    current.update(wall=900, monotonic=1.05)
    with pytest.raises(ClockSkewError, match="skew policy"):
        pool.available("worker")
    assert pool.validate_fencing_token(lease.lease_id, lease.fencing_token) is None

    current.update(wall=1_200, monotonic=1.2)
    assert pool.available("worker") == 1


def test_audit_wall_clock_normalizes_aware_datetimes_to_utc() -> None:
    clock = AuditWallClock(
        utc_datetime=lambda: datetime(
            2026,
            8,
            8,
            18,
            30,
            45,
            123_456,
            tzinfo=timezone(timedelta(hours=9)),
        )
    )

    assert clock.now_timestamp() == "2026-08-08T09:30:45.123Z"


def test_audit_wall_clock_rejects_naive_datetimes() -> None:
    clock = AuditWallClock(
        utc_datetime=lambda: datetime(2026, 8, 8, 9, 30),
    )

    with pytest.raises(ClockValueError, match="timezone-aware"):
        clock.now_timestamp()

    invalid_type = AuditWallClock(
        utc_datetime=lambda: "2026-08-08T09:30:00Z",  # type: ignore[return-value]
    )
    with pytest.raises(ClockValueError, match="must return a datetime"):
        invalid_type.now_timestamp()
    with pytest.raises(ClockValueError, match="must be callable"):
        AuditWallClock(utc_datetime=object())  # type: ignore[arg-type]


def test_system_clock_adapters_return_their_declared_domains() -> None:
    authority = AuthorityWallClock(max_skew_milliseconds=30_000)

    assert system_authority_wall_milliseconds() >= 0
    assert authority() >= 0
    assert system_audit_wall_timestamp().endswith("Z")


def test_server_uses_guarded_authority_and_injected_monotonic_clocks() -> None:
    process_clock = ScriptedClock(10.0, 16.0)
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        monotonic_clock=process_clock,  # type: ignore[arg-type]
    )
    app._active_server_requests = 1

    assert isinstance(app.admission_clock, AuthorityWallClock)
    assert app.drain(timeout=5) is False
    app._active_server_requests = 0
    assert app.close() is True
