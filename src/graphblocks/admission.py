from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Literal


AdmissionTicketState = Literal[
    "queued",
    "admitted",
    "running",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
TERMINAL_ADMISSION_TICKET_STATES = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)
_MAX_ADMISSION_INTEGER = (1 << 64) - 1


class AdmissionError(ValueError):
    """Base error for admission-ticket contracts."""


class AdmissionQueueFullError(AdmissionError):
    def __init__(self, limiter_id: str, max_pending: int) -> None:
        self.limiter_id = limiter_id
        self.max_pending = max_pending
        super().__init__(
            f"admission queue {limiter_id!r} reached max_pending {max_pending}"
        )


class AdmissionIdempotencyConflictError(AdmissionError):
    def __init__(self, owner_id: str, request_id: str) -> None:
        self.owner_id = owner_id
        self.request_id = request_id
        super().__init__(
            f"admission request {request_id!r} for owner {owner_id!r} conflicts with its existing ticket"
        )


class AdmissionTicketNotFoundError(AdmissionError):
    def __init__(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"admission ticket {ticket_id!r} was not found")


class AdmissionStaleFencingTokenError(AdmissionError):
    def __init__(self, ticket_id: str, expected: int | None, actual: int) -> None:
        self.ticket_id = ticket_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"admission ticket {ticket_id!r} fencing token mismatch: expected {expected}, got {actual}"
        )


class AdmissionTicketStateError(AdmissionError):
    def __init__(self, ticket_id: str, state: str, operation: str) -> None:
        self.ticket_id = ticket_id
        self.state = state
        self.operation = operation
        super().__init__(
            f"admission ticket {ticket_id!r} in state {state!r} cannot {operation}"
        )


def _non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise AdmissionError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise AdmissionError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise AdmissionError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise AdmissionError(
            f"{owner} {field_name} must not contain control characters"
        )
    return value


def _non_negative_integer(owner: str, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdmissionError(f"{owner} {field_name} must be an integer")
    if not 0 <= value <= _MAX_ADMISSION_INTEGER:
        raise AdmissionError(
            f"{owner} {field_name} must be between 0 and {_MAX_ADMISSION_INTEGER}"
        )
    return value


def _positive_integer(owner: str, field_name: str, value: object) -> int:
    parsed = _non_negative_integer(owner, field_name, value)
    if parsed == 0:
        raise AdmissionError(f"{owner} {field_name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class AdmissionTicket:
    ticket_id: str
    run_id: str
    request_id: str
    owner_id: str
    limiter_id: str
    state: AdmissionTicketState
    units: int
    sequence: int
    state_version: int
    issued_at_ms: int
    expires_at_ms: int
    queue_position: int | None = None
    retry_after_ms: int | None = None
    fencing_token: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "ticket_id",
            "run_id",
            "request_id",
            "owner_id",
            "limiter_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_empty_string("admission ticket", field_name, getattr(self, field_name)),
            )
        if self.state not in {
            "queued",
            "admitted",
            "running",
            "completed",
            "failed",
            "cancelled",
            "expired",
        }:
            raise AdmissionError(f"admission ticket has invalid state {self.state!r}")
        _positive_integer("admission ticket", "units", self.units)
        _positive_integer("admission ticket", "sequence", self.sequence)
        _positive_integer("admission ticket", "state_version", self.state_version)
        _non_negative_integer("admission ticket", "issued_at_ms", self.issued_at_ms)
        _non_negative_integer("admission ticket", "expires_at_ms", self.expires_at_ms)
        if self.expires_at_ms <= self.issued_at_ms:
            raise AdmissionError("admission ticket expires_at_ms must be after issued_at_ms")
        for field_name in (
            "queue_position",
            "retry_after_ms",
            "fencing_token",
            "started_at_ms",
            "completed_at_ms",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if field_name in {"queue_position", "fencing_token"}:
                    _positive_integer("admission ticket", field_name, value)
                else:
                    _non_negative_integer("admission ticket", field_name, value)
        if self.state == "queued":
            if self.queue_position is None or self.fencing_token is not None:
                raise AdmissionError(
                    "queued admission ticket requires queue_position and no fencing_token"
                )
        elif self.queue_position is not None:
            raise AdmissionError("non-queued admission ticket must not have queue_position")
        if self.state != "queued" and self.retry_after_ms is not None:
            raise AdmissionError(
                "non-queued admission ticket must not have retry_after_ms"
            )
        if self.state in {"admitted", "running"} and self.fencing_token is None:
            raise AdmissionError(
                "admitted or running admission ticket requires fencing_token"
            )
        if self.state == "running" and self.started_at_ms is None:
            raise AdmissionError("running admission ticket requires started_at_ms")
        if self.state in TERMINAL_ADMISSION_TICKET_STATES and self.completed_at_ms is None:
            raise AdmissionError("terminal admission ticket requires completed_at_ms")
        if self.state in {"queued", "admitted"} and self.started_at_ms is not None:
            raise AdmissionError(
                "queued or admitted admission ticket must not have started_at_ms"
            )
        if self.state not in TERMINAL_ADMISSION_TICKET_STATES and self.completed_at_ms is not None:
            raise AdmissionError(
                "non-terminal admission ticket must not have completed_at_ms"
            )
        if self.started_at_ms is not None and self.started_at_ms < self.issued_at_ms:
            raise AdmissionError(
                "admission ticket started_at_ms must not precede issued_at_ms"
            )
        if self.completed_at_ms is not None:
            lower_bound = (
                self.issued_at_ms
                if self.started_at_ms is None
                else self.started_at_ms
            )
            if self.completed_at_ms < lower_bound:
                raise AdmissionError(
                    "admission ticket completed_at_ms must not precede its lifecycle"
                )

    def contract(self) -> dict[str, object]:
        """Return the client-safe ticket projection.

        Ownership and fencing data stay process-internal because neither is a
        client capability.
        """

        return {
            "ticketId": self.ticket_id,
            "runId": self.run_id,
            "limiterId": self.limiter_id,
            "state": self.state,
            "units": self.units,
            "sequence": self.sequence,
            "stateVersion": self.state_version,
            "issuedAtUnixMs": self.issued_at_ms,
            "expiresAtUnixMs": self.expires_at_ms,
            "queuePosition": self.queue_position,
            "retryAfterMs": self.retry_after_ms,
            "startedAtUnixMs": self.started_at_ms,
            "completedAtUnixMs": self.completed_at_ms,
        }


@dataclass(frozen=True, slots=True)
class AdmissionSubmission:
    ticket: AdmissionTicket
    duplicate: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.ticket, AdmissionTicket):
            raise AdmissionError(
                "admission submission ticket must be an AdmissionTicket"
            )
        if not isinstance(self.duplicate, bool):
            raise AdmissionError(
                "admission submission duplicate must be a boolean"
            )


@dataclass(slots=True)
class AdmissionTicketQueue:
    limiter_id: str
    max_concurrent: int
    rate_limit: int
    window_ms: int
    max_pending: int
    ticket_ttl_ms: int
    max_terminal_tickets: int = 1_024
    _tickets: dict[str, AdmissionTicket] = field(default_factory=dict, init=False, repr=False)
    _request_tickets: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _pending: deque[str] = field(default_factory=deque, init=False, repr=False)
    _active: set[str] = field(default_factory=set, init=False, repr=False)
    _window_start_ms: int | None = field(default=None, init=False, repr=False)
    _window_used: int = field(default=0, init=False, repr=False)
    _next_ticket: int = field(default=1, init=False, repr=False)
    _next_sequence: int = field(default=1, init=False, repr=False)
    _next_fencing_token: int = field(default=1, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.limiter_id = _non_empty_string(
            "admission queue", "limiter_id", self.limiter_id
        )
        for field_name in (
            "max_concurrent",
            "rate_limit",
            "window_ms",
            "max_pending",
            "ticket_ttl_ms",
            "max_terminal_tickets",
        ):
            setattr(
                self,
                field_name,
                _positive_integer("admission queue", field_name, getattr(self, field_name)),
            )

    def submit(
        self,
        run_id: str,
        request_id: str,
        owner_id: str,
        *,
        now_ms: int,
        units: int = 1,
    ) -> AdmissionSubmission:
        run_id = _non_empty_string("admission request", "run_id", run_id)
        request_id = _non_empty_string("admission request", "request_id", request_id)
        owner_id = _non_empty_string("admission request", "owner_id", owner_id)
        now_ms = _non_negative_integer("admission request", "now_ms", now_ms)
        units = _positive_integer("admission request", "units", units)
        if units > self.rate_limit:
            raise AdmissionError("admission request units must not exceed rate_limit")
        if now_ms > _MAX_ADMISSION_INTEGER - self.ticket_ttl_ms:
            raise AdmissionError(
                "admission request expiry exceeds the supported integer range"
            )

        with self._lock:
            request_key = (owner_id, request_id)
            existing_id = self._request_tickets.get(request_key)
            if existing_id is not None:
                existing = self._tickets[existing_id]
                if existing.run_id != run_id or existing.units != units:
                    raise AdmissionIdempotencyConflictError(owner_id, request_id)
                self._validate_mutation_time_locked(existing, now_ms, "resubmit")
            self._expire_locked(now_ms)
            self._promote_locked(now_ms)
            existing_id = self._request_tickets.get(request_key)
            if existing_id is not None:
                existing = self._tickets[existing_id]
                if existing.state in {"expired", "failed"}:
                    self._evict_terminal_locked(existing_id)
                elif self._terminal_retention_expired(existing, now_ms):
                    self._evict_terminal_locked(existing_id)
                else:
                    return AdmissionSubmission(existing, duplicate=True)
            self._prune_terminal_locked(now_ms)

            self._refresh_window_locked(now_ms)
            can_admit = (
                not self._pending
                and len(self._active) < self.max_concurrent
                and self._window_used + units <= self.rate_limit
            )
            if not can_admit and len(self._pending) >= self.max_pending:
                raise AdmissionQueueFullError(self.limiter_id, self.max_pending)

            ticket_id = f"{self.limiter_id}-ticket-{self._next_ticket:06d}"
            next_ticket = _increment_admission_counter(
                "ticket",
                self._next_ticket,
            )
            sequence = self._next_sequence
            next_sequence = _increment_admission_counter(
                "sequence",
                self._next_sequence,
            )
            fencing_token = None
            state: AdmissionTicketState = "queued"
            retry_after_ms = None
            queue_position = len(self._pending) + 1
            if can_admit:
                state = "admitted"
                queue_position = None
                fencing_token = self._next_fencing_token
                next_fencing_token = _increment_admission_counter(
                    "fencing token",
                    self._next_fencing_token,
                )
                self._next_fencing_token = next_fencing_token
                self._active.add(ticket_id)
                self._window_used += units
            elif self._window_used + units > self.rate_limit:
                assert self._window_start_ms is not None
                retry_after_ms = max(
                    0,
                    self._window_start_ms + self.window_ms - now_ms,
                )

            self._next_ticket = next_ticket
            self._next_sequence = next_sequence
            ticket = AdmissionTicket(
                ticket_id=ticket_id,
                run_id=run_id,
                request_id=request_id,
                owner_id=owner_id,
                limiter_id=self.limiter_id,
                state=state,
                units=units,
                sequence=sequence,
                state_version=1,
                issued_at_ms=now_ms,
                expires_at_ms=now_ms + self.ticket_ttl_ms,
                queue_position=queue_position,
                retry_after_ms=retry_after_ms,
                fencing_token=fencing_token,
            )
            self._tickets[ticket_id] = ticket
            self._request_tickets[(owner_id, request_id)] = ticket_id
            if state == "queued":
                self._pending.append(ticket_id)
                self._reindex_locked(now_ms)
                ticket = self._tickets[ticket_id]
            return AdmissionSubmission(ticket)

    def get(self, ticket_id: str) -> AdmissionTicket:
        ticket_id = _non_empty_string("admission queue", "ticket_id", ticket_id)
        with self._lock:
            try:
                return self._tickets[ticket_id]
            except KeyError as error:
                raise AdmissionTicketNotFoundError(ticket_id) from error

    def promote(self, *, now_ms: int) -> tuple[AdmissionTicket, ...]:
        now_ms = _non_negative_integer("admission queue", "now_ms", now_ms)
        with self._lock:
            self._expire_locked(now_ms)
            promoted = self._promote_locked(now_ms)
            self._prune_terminal_locked(now_ms)
            return promoted

    def mark_running(
        self,
        ticket_id: str,
        fencing_token: int,
        *,
        now_ms: int,
    ) -> AdmissionTicket:
        ticket_id = _non_empty_string("admission queue", "ticket_id", ticket_id)
        fencing_token = _positive_integer(
            "admission queue", "fencing_token", fencing_token
        )
        now_ms = _non_negative_integer("admission queue", "now_ms", now_ms)
        with self._lock:
            ticket = self.get(ticket_id)
            self._validate_mutation_time_locked(ticket, now_ms, "start")
            self._expire_locked(now_ms)
            ticket = self.get(ticket_id)
            self._validate_fencing_locked(ticket, fencing_token)
            if ticket.state == "running":
                return ticket
            if ticket.state != "admitted":
                raise AdmissionTicketStateError(ticket_id, ticket.state, "start")
            running = replace(
                ticket,
                state="running",
                state_version=_increment_admission_counter(
                    "state version",
                    ticket.state_version,
                ),
                started_at_ms=now_ms,
            )
            self._tickets[ticket_id] = running
            self._prune_terminal_locked(now_ms)
            return running

    def complete(
        self,
        ticket_id: str,
        fencing_token: int,
        state: Literal["completed", "failed"],
        *,
        now_ms: int,
    ) -> tuple[AdmissionTicket, tuple[AdmissionTicket, ...]]:
        ticket_id = _non_empty_string("admission queue", "ticket_id", ticket_id)
        fencing_token = _positive_integer(
            "admission queue", "fencing_token", fencing_token
        )
        now_ms = _non_negative_integer("admission queue", "now_ms", now_ms)
        if state not in {"completed", "failed"}:
            raise AdmissionError("admission completion state must be completed or failed")
        with self._lock:
            ticket = self.get(ticket_id)
            self._validate_mutation_time_locked(ticket, now_ms, "complete")
            self._expire_locked(now_ms)
            ticket = self.get(ticket_id)
            self._validate_fencing_locked(ticket, fencing_token)
            if ticket.state not in {"admitted", "running"}:
                raise AdmissionTicketStateError(ticket_id, ticket.state, "complete")
            terminal = replace(
                ticket,
                state=state,
                state_version=_increment_admission_counter(
                    "state version",
                    ticket.state_version,
                ),
                completed_at_ms=now_ms,
            )
            self._tickets[ticket_id] = terminal
            self._active.discard(ticket_id)
            promoted = self._promote_locked(now_ms)
            self._prune_terminal_locked(now_ms)
            return terminal, promoted

    def cancel(
        self,
        ticket_id: str,
        *,
        now_ms: int,
        state: Literal["cancelled", "expired"] = "cancelled",
        fencing_token: int | None = None,
    ) -> tuple[AdmissionTicket, tuple[AdmissionTicket, ...]]:
        ticket_id = _non_empty_string("admission queue", "ticket_id", ticket_id)
        now_ms = _non_negative_integer("admission queue", "now_ms", now_ms)
        if state not in {"cancelled", "expired"}:
            raise AdmissionError("admission cancellation state must be cancelled or expired")
        if fencing_token is not None:
            fencing_token = _positive_integer(
                "admission queue",
                "fencing_token",
                fencing_token,
            )
        with self._lock:
            ticket = self.get(ticket_id)
            self._validate_mutation_time_locked(ticket, now_ms, "cancel")
            self._expire_locked(now_ms)
            ticket = self.get(ticket_id)
            if ticket.state in TERMINAL_ADMISSION_TICKET_STATES:
                self._prune_terminal_locked(now_ms)
                return ticket, ()
            if ticket.state == "running":
                if fencing_token is None:
                    raise AdmissionTicketStateError(
                        ticket_id,
                        ticket.state,
                        "cancel without a post-worker fencing token",
                    )
                self._validate_fencing_locked(ticket, fencing_token)
            state_version = _increment_admission_counter(
                "state version",
                ticket.state_version,
            )
            if ticket.state == "queued":
                self._pending.remove(ticket_id)
            else:
                self._active.discard(ticket_id)
            cancelled = replace(
                ticket,
                state=state,
                state_version=state_version,
                queue_position=None,
                retry_after_ms=None,
                completed_at_ms=now_ms,
            )
            self._tickets[ticket_id] = cancelled
            self._reindex_locked(now_ms)
            promoted = self._promote_locked(now_ms)
            self._prune_terminal_locked(now_ms)
            return cancelled, promoted

    def expire(self, *, now_ms: int) -> tuple[AdmissionTicket, ...]:
        now_ms = _non_negative_integer("admission queue", "now_ms", now_ms)
        with self._lock:
            expired = self._expire_locked(now_ms)
            self._prune_terminal_locked(now_ms)
            return expired

    def pending_ticket_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending)

    def _validate_fencing_locked(
        self,
        ticket: AdmissionTicket,
        fencing_token: int,
    ) -> None:
        if ticket.fencing_token != fencing_token:
            raise AdmissionStaleFencingTokenError(
                ticket.ticket_id,
                ticket.fencing_token,
                fencing_token,
            )

    def _validate_mutation_time_locked(
        self,
        ticket: AdmissionTicket,
        now_ms: int,
        operation: str,
    ) -> None:
        if now_ms < ticket.issued_at_ms:
            raise AdmissionTicketStateError(
                ticket.ticket_id,
                ticket.state,
                f"{operation} before issuance",
            )

    def _refresh_window_locked(self, now_ms: int) -> None:
        if (
            self._window_start_ms is None
            or now_ms >= self._window_start_ms + self.window_ms
        ):
            self._window_start_ms = now_ms
            self._window_used = 0

    def _promote_locked(self, now_ms: int) -> tuple[AdmissionTicket, ...]:
        self._refresh_window_locked(now_ms)
        promoted: list[AdmissionTicket] = []
        while self._pending and len(self._active) < self.max_concurrent:
            ticket_id = self._pending[0]
            ticket = self._tickets[ticket_id]
            if now_ms < ticket.issued_at_ms:
                break
            if self._window_used + ticket.units > self.rate_limit:
                break
            state_version = _increment_admission_counter(
                "state version",
                ticket.state_version,
            )
            fencing_token = self._next_fencing_token
            next_fencing_token = _increment_admission_counter(
                "fencing token",
                self._next_fencing_token,
            )
            self._pending.popleft()
            self._next_fencing_token = next_fencing_token
            admitted = replace(
                ticket,
                state="admitted",
                state_version=state_version,
                queue_position=None,
                retry_after_ms=None,
                fencing_token=fencing_token,
            )
            self._tickets[ticket_id] = admitted
            self._active.add(ticket_id)
            self._window_used += ticket.units
            promoted.append(admitted)
        self._reindex_locked(now_ms)
        return tuple(promoted)

    def _expire_locked(self, now_ms: int) -> tuple[AdmissionTicket, ...]:
        expired: list[AdmissionTicket] = []
        for ticket_id, ticket in tuple(self._tickets.items()):
            if ticket.state not in {"queued", "admitted", "running"} or ticket.expires_at_ms > now_ms:
                continue
            state_version = _increment_admission_counter(
                "state version",
                ticket.state_version,
            )
            if ticket.state == "queued":
                self._pending.remove(ticket_id)
            else:
                self._active.discard(ticket_id)
            terminal = replace(
                ticket,
                state="expired",
                state_version=state_version,
                queue_position=None,
                retry_after_ms=None,
                completed_at_ms=now_ms,
            )
            self._tickets[ticket_id] = terminal
            expired.append(terminal)
        self._reindex_locked(now_ms)
        self._prune_terminal_locked(now_ms)
        return tuple(sorted(expired, key=lambda ticket: ticket.sequence))

    def _prune_terminal_locked(self, now_ms: int) -> None:
        terminal_tickets = sorted(
            (
                ticket
                for ticket in self._tickets.values()
                if ticket.state in TERMINAL_ADMISSION_TICKET_STATES
            ),
            key=lambda ticket: (ticket.completed_at_ms or 0, ticket.sequence),
        )
        for ticket in terminal_tickets:
            if self._terminal_retention_expired(ticket, now_ms):
                self._evict_terminal_locked(ticket.ticket_id)

        retained_terminal_tickets = [
            ticket
            for ticket in terminal_tickets
            if ticket.ticket_id in self._tickets
        ]
        overflow = len(retained_terminal_tickets) - self.max_terminal_tickets
        for ticket in retained_terminal_tickets[: max(overflow, 0)]:
            self._evict_terminal_locked(ticket.ticket_id)

    def _terminal_retention_expired(
        self,
        ticket: AdmissionTicket,
        now_ms: int,
    ) -> bool:
        return (
            ticket.state in TERMINAL_ADMISSION_TICKET_STATES
            and ticket.completed_at_ms is not None
            and ticket.completed_at_ms <= now_ms - self.ticket_ttl_ms
        )

    def _evict_terminal_locked(self, ticket_id: str) -> None:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.state not in TERMINAL_ADMISSION_TICKET_STATES:
            return
        self._tickets.pop(ticket_id, None)
        request_key = (ticket.owner_id, ticket.request_id)
        if self._request_tickets.get(request_key) == ticket_id:
            self._request_tickets.pop(request_key, None)

    def _reindex_locked(self, now_ms: int) -> None:
        self._refresh_window_locked(now_ms)
        projected_used = self._window_used
        for position, ticket_id in enumerate(self._pending, start=1):
            ticket = self._tickets[ticket_id]
            retry_after_ms = None
            if projected_used + ticket.units > self.rate_limit:
                assert self._window_start_ms is not None
                retry_after_ms = max(
                    0,
                    self._window_start_ms + self.window_ms - now_ms,
                )
            if (
                ticket.queue_position != position
                or ticket.retry_after_ms != retry_after_ms
            ):
                self._tickets[ticket_id] = replace(
                    ticket,
                    state_version=_increment_admission_counter(
                        "state version",
                        ticket.state_version,
                    ),
                    queue_position=position,
                    retry_after_ms=retry_after_ms,
                )
            projected_used += ticket.units


def _increment_admission_counter(field_name: str, value: int) -> int:
    if value >= _MAX_ADMISSION_INTEGER:
        raise AdmissionError(
            f"admission queue {field_name} counter is exhausted"
        )
    return value + 1
