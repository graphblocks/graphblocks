from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from threading import Lock
from time import monotonic, time


DEFAULT_MAX_AUTHORITY_CLOCK_SKEW_MILLISECONDS = 30_000


class ClockValueError(ValueError):
    """A configured clock returned a value outside its declared domain."""


class ClockSkewError(RuntimeError):
    """An authority clock diverged from elapsed monotonic time."""


def _finite_non_negative_seconds(owner: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockValueError(f"{owner} must return a number")
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ClockValueError(f"{owner} must return a finite number") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise ClockValueError(f"{owner} must return a finite non-negative number")
    return seconds


def _authority_milliseconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClockValueError(
            "authority wall clock must return integer epoch milliseconds"
        )
    if value < 0:
        raise ClockValueError(
            "authority wall clock must return non-negative epoch milliseconds"
        )
    return value


def system_authority_wall_milliseconds() -> int:
    """Return system wall time for persistent authority comparisons."""

    return int(time() * 1_000)


def _system_utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class AuditWallClock:
    """Format human/audit wall time without using it as a deadline."""

    utc_datetime: Callable[[], datetime] = field(
        default=_system_utc_datetime,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(self.utc_datetime):
            raise ClockValueError("audit wall clock must be callable")

    def now_timestamp(self) -> str:
        value = self.utc_datetime()
        if not isinstance(value, datetime):
            raise ClockValueError("audit wall clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ClockValueError("audit wall clock datetime must be timezone-aware")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


_SYSTEM_AUDIT_WALL_CLOCK = AuditWallClock()


def system_audit_wall_timestamp() -> str:
    """Return a UTC audit timestamp that carries no deadline authority."""

    return _SYSTEM_AUDIT_WALL_CLOCK.now_timestamp()


@dataclass(frozen=True, slots=True)
class MonotonicDeadline:
    """A process-local deadline that is isolated from wall-clock changes."""

    expires_at_seconds: float | None
    clock: Callable[[], float] = field(
        default=monotonic,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(self.clock):
            raise ClockValueError("monotonic clock must be callable")
        if self.expires_at_seconds is not None:
            object.__setattr__(
                self,
                "expires_at_seconds",
                _finite_non_negative_seconds(
                    "monotonic deadline expiration",
                    self.expires_at_seconds,
                ),
            )

    @classmethod
    def after(
        cls,
        timeout_seconds: float | None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> MonotonicDeadline:
        if not callable(clock):
            raise ClockValueError("monotonic clock must be callable")
        if timeout_seconds is None:
            return cls(None, clock)
        timeout = _finite_non_negative_seconds(
            "monotonic deadline timeout",
            timeout_seconds,
        )
        now = _finite_non_negative_seconds("monotonic clock", clock())
        return cls(now + timeout, clock)

    def remaining_seconds(self) -> float | None:
        if self.expires_at_seconds is None:
            return None
        now = _finite_non_negative_seconds("monotonic clock", self.clock())
        return max(0.0, self.expires_at_seconds - now)


@dataclass(slots=True)
class AuthorityWallClock:
    """Guard persistent epoch milliseconds against local wall-clock skew."""

    wall_milliseconds: Callable[[], int] = field(
        default=system_authority_wall_milliseconds,
        repr=False,
        compare=False,
    )
    monotonic_seconds: Callable[[], float] = field(
        default=monotonic,
        repr=False,
        compare=False,
    )
    max_skew_milliseconds: int = DEFAULT_MAX_AUTHORITY_CLOCK_SKEW_MILLISECONDS
    _last_wall_milliseconds: int | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_monotonic_seconds: float | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(self.wall_milliseconds):
            raise ClockValueError("authority wall clock must be callable")
        if not callable(self.monotonic_seconds):
            raise ClockValueError("authority monotonic clock must be callable")
        if (
            isinstance(self.max_skew_milliseconds, bool)
            or not isinstance(self.max_skew_milliseconds, int)
            or self.max_skew_milliseconds < 0
        ):
            raise ClockValueError(
                "authority max_skew_milliseconds must be a non-negative integer"
            )

    def now_milliseconds(self) -> int:
        with self._lock:
            wall = _authority_milliseconds(self.wall_milliseconds())
            observed_monotonic = _finite_non_negative_seconds(
                "authority monotonic clock",
                self.monotonic_seconds(),
            )
            if self._last_wall_milliseconds is None:
                self._last_wall_milliseconds = wall
                self._last_monotonic_seconds = observed_monotonic
                return wall

            assert self._last_monotonic_seconds is not None
            elapsed_seconds = observed_monotonic - self._last_monotonic_seconds
            if elapsed_seconds < 0:
                raise ClockSkewError("authority monotonic clock moved backwards")
            expected_wall = self._last_wall_milliseconds + elapsed_seconds * 1_000
            skew_milliseconds = wall - expected_wall
            if abs(skew_milliseconds) > self.max_skew_milliseconds:
                raise ClockSkewError(
                    "authority wall clock exceeded the configured skew policy"
                )

            effective_wall = max(wall, self._last_wall_milliseconds)
            self._last_wall_milliseconds = effective_wall
            self._last_monotonic_seconds = observed_monotonic
            return effective_wall

    def __call__(self) -> int:
        return self.now_milliseconds()


__all__ = [
    "AuditWallClock",
    "AuthorityWallClock",
    "ClockSkewError",
    "ClockValueError",
    "DEFAULT_MAX_AUTHORITY_CLOCK_SKEW_MILLISECONDS",
    "MonotonicDeadline",
    "system_audit_wall_timestamp",
    "system_authority_wall_milliseconds",
]
