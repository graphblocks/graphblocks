from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .application_event import ApplicationEvent, ApplicationProtocolEvent


CallbackSubscriptionScope = Literal["run", "conversation", "project", "tenant", "deployment"]
CallbackSubscriptionStatus = Literal["active", "paused", "expired", "revoked"]
CallbackFailurePolicy = Literal[
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
]
CallbackDeliveryStatus = Literal[
    "pending",
    "delivering",
    "delivered",
    "acknowledged",
    "failed",
    "dead_lettered",
    "cancelled",
    "expired",
]

VALID_CALLBACK_SUBSCRIPTION_SCOPES = frozenset({"run", "conversation", "project", "tenant", "deployment"})
VALID_CALLBACK_SUBSCRIPTION_STATUSES = frozenset({"active", "paused", "expired", "revoked"})
VALID_CALLBACK_FAILURE_POLICIES = frozenset({
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
})
VALID_CALLBACK_DELIVERY_STATUSES = frozenset({
    "pending",
    "delivering",
    "delivered",
    "acknowledged",
    "failed",
    "dead_lettered",
    "cancelled",
    "expired",
})
VALID_EVENT_VISIBILITIES = frozenset({"client", "operator", "internal", "audit_only"})
EVENT_SEVERITY_RANKS = {
    "debug": 10,
    "info": 20,
    "notice": 30,
    "warning": 40,
    "warn": 40,
    "error": 50,
    "critical": 60,
    "fatal": 60,
}
TERMINAL_APPLICATION_EVENT_KINDS = frozenset({
    "RunSucceeded",
    "RunFailed",
    "RunCancelled",
    "RunPolicyStopped",
    "RunCompleted",
    "RunExpired",
})
TERMINAL_CALLBACK_DELIVERY_STATUSES = frozenset({
    "delivered",
    "acknowledged",
    "dead_lettered",
    "cancelled",
    "expired",
})


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _validate_non_negative_int(owner: str, field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{owner} {field_name} must be a non-negative integer")
    return value


def _validate_positive_int(owner: str, field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{owner} {field_name} must be a positive integer")
    return value


def _parse_iso_datetime(owner: str, field_name: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    if len(value) <= 19 or value[10] != "T":
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    suffix_start = 19
    if value[suffix_start] == ".":
        suffix_start += 1
        fraction_start = suffix_start
        while suffix_start < len(value) and value[suffix_start].isdigit():
            suffix_start += 1
        if suffix_start == fraction_start:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    timezone_suffix = value[suffix_start:]
    if value.endswith("Z"):
        normalized = f"{value[:-1]}+00:00"
    elif (
        len(timezone_suffix) != 6
        or timezone_suffix[0] not in {"+", "-"}
        or timezone_suffix[3] != ":"
        or not timezone_suffix[1:3].isdigit()
        or not timezone_suffix[4:6].isdigit()
        or int(timezone_suffix[1:3]) > 23
        or int(timezone_suffix[4:6]) > 59
    ):
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    else:
        normalized = value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    return parsed.astimezone(timezone.utc)


def _normalize_iso_datetime(owner: str, field_name: str, value: object) -> str:
    parsed = _parse_iso_datetime(owner, field_name, value)
    return parsed.isoformat().replace("+00:00", "Z")


def _optional_non_empty_string(owner: str, field_name: str, value: object) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _optional_iso_datetime(owner: str, field_name: str, value: object) -> str | None:
    if value is None:
        return None
    return _normalize_iso_datetime(owner, field_name, value)


def _string_tuple(owner: str, field_name: str, value: Iterable[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray, memoryview, Mapping)):
        raise ValueError(f"{owner} {field_name} must be a sequence")
    try:
        items = tuple(value)
    except TypeError:
        raise ValueError(f"{owner} {field_name} must be a sequence") from None
    normalized = tuple(_validate_non_empty_string(owner, field_name, item) for item in items)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} {field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class EventFilter:
    types: tuple[str, ...] | None = None
    visibility: tuple[str, ...] | None = None
    node_ids: tuple[str, ...] | None = None
    operation_ids: tuple[str, ...] | None = None
    severity_min: str | None = None
    include_terminal_events: bool = True

    def __init__(
        self,
        *,
        types: Iterable[str] | None = None,
        visibility: Iterable[str] | None = None,
        node_ids: Iterable[str] | None = None,
        operation_ids: Iterable[str] | None = None,
        severity_min: str | None = None,
        include_terminal_events: bool = True,
    ) -> None:
        object.__setattr__(self, "types", _string_tuple("event filter", "types", types))
        normalized_visibility = _string_tuple("event filter", "visibility", visibility)
        if normalized_visibility is not None and any(item not in VALID_EVENT_VISIBILITIES for item in normalized_visibility):
            raise ValueError("event filter visibility must contain only valid visibility values")
        object.__setattr__(self, "visibility", normalized_visibility)
        object.__setattr__(self, "node_ids", _string_tuple("event filter", "node_ids", node_ids))
        object.__setattr__(self, "operation_ids", _string_tuple("event filter", "operation_ids", operation_ids))
        normalized_severity = _optional_non_empty_string(
            "event filter",
            "severity_min",
            severity_min,
        )
        if (
            normalized_severity is not None
            and normalized_severity not in EVENT_SEVERITY_RANKS
        ):
            raise ValueError("event filter severity_min must be a valid severity")
        object.__setattr__(self, "severity_min", normalized_severity)
        if not isinstance(include_terminal_events, bool):
            raise ValueError("event filter include_terminal_events must be a boolean")
        object.__setattr__(self, "include_terminal_events", include_terminal_events)

    def to_json(self) -> dict[str, object]:
        return {
            "types": list(self.types) if self.types is not None else None,
            "visibility": list(self.visibility) if self.visibility is not None else None,
            "node_ids": list(self.node_ids) if self.node_ids is not None else None,
            "operation_ids": list(self.operation_ids) if self.operation_ids is not None else None,
            "severity_min": self.severity_min,
            "include_terminal_events": self.include_terminal_events,
        }

    def authorized_for_visibility(self, allowed_visibility: Iterable[str]) -> EventFilter:
        allowed = _string_tuple("event filter", "authorized visibility", allowed_visibility)
        if allowed is None:
            raise ValueError("event filter authorized visibility must be a sequence")
        if any(item not in VALID_EVENT_VISIBILITIES for item in allowed):
            raise ValueError("event filter authorized visibility must contain only valid visibility values")
        if self.visibility is None:
            constrained_visibility = allowed
        else:
            allowed_set = set(allowed)
            constrained_visibility = tuple(item for item in self.visibility if item in allowed_set)
        return EventFilter(
            types=self.types,
            visibility=constrained_visibility,
            node_ids=self.node_ids,
            operation_ids=self.operation_ids,
            severity_min=self.severity_min,
            include_terminal_events=self.include_terminal_events,
        )

    def matches(self, event: ApplicationEvent | ApplicationProtocolEvent) -> bool:
        if not isinstance(event, (ApplicationEvent, ApplicationProtocolEvent)):
            raise ValueError("event filter event must be an ApplicationEvent or ApplicationProtocolEvent")
        if isinstance(event, ApplicationEvent):
            visibility = event.metadata.visibility
            node_id = event.metadata.node_id
            operation_id = event.metadata.operation_id
        else:
            payload_visibility = event.payload.get("visibility")
            payload_node_id = event.payload.get("node_id", event.payload.get("nodeId"))
            if "visibility" not in event.payload:
                visibility = "client"
            else:
                visibility = payload_visibility if isinstance(payload_visibility, str) else None
            node_id = payload_node_id if isinstance(payload_node_id, str) else None
            operation_id = event.metadata.operation_id
            if operation_id is None:
                payload_operation_id = event.payload.get("operation_id", event.payload.get("operationId"))
                operation_id = payload_operation_id if isinstance(payload_operation_id, str) else None
        if self.visibility is not None and visibility not in self.visibility:
            return False
        if self.node_ids is not None and node_id not in self.node_ids:
            return False
        if self.operation_ids is not None and operation_id not in self.operation_ids:
            return False
        if self.severity_min is not None:
            minimum_rank = EVENT_SEVERITY_RANKS.get(self.severity_min)
            event_severity = event.payload.get("severity")
            event_rank = EVENT_SEVERITY_RANKS.get(event_severity) if isinstance(event_severity, str) else None
            if minimum_rank is None or event_rank is None or event_rank < minimum_rank:
                return False
        if event.kind in TERMINAL_APPLICATION_EVENT_KINDS and not self.include_terminal_events:
            return False
        if self.types is not None and event.kind not in self.types:
            return False
        return True


@dataclass(frozen=True, slots=True)
class CallbackSubscription:
    subscription_id: str
    owner: str
    scope: CallbackSubscriptionScope
    scope_id: str
    event_filter: EventFilter
    delivery_target: str
    status: CallbackSubscriptionStatus
    created_at: str
    expires_at: str | None = None
    replay_from_cursor: str | None = None
    failure_policy: CallbackFailurePolicy = "retry_then_dead_letter"

    def __post_init__(self) -> None:
        for field_name in ("subscription_id", "owner", "scope_id", "delivery_target"):
            object.__setattr__(
                self,
                field_name,
                _validate_non_empty_string("callback subscription", field_name, getattr(self, field_name)),
            )
        scope = _validate_non_empty_string("callback subscription", "scope", self.scope)
        if scope not in VALID_CALLBACK_SUBSCRIPTION_SCOPES:
            raise ValueError("callback subscription scope must be one of run, conversation, project, tenant, or deployment")
        object.__setattr__(self, "scope", scope)
        status = _validate_non_empty_string("callback subscription", "status", self.status)
        if status not in VALID_CALLBACK_SUBSCRIPTION_STATUSES:
            raise ValueError("callback subscription status must be one of active, paused, expired, or revoked")
        object.__setattr__(self, "status", status)
        failure_policy = _validate_non_empty_string("callback subscription", "failure_policy", self.failure_policy)
        if failure_policy not in VALID_CALLBACK_FAILURE_POLICIES:
            raise ValueError("callback subscription failure_policy must be a valid callback failure policy")
        object.__setattr__(self, "failure_policy", failure_policy)
        if not isinstance(self.event_filter, EventFilter):
            raise ValueError("callback subscription event_filter must be an EventFilter")
        object.__setattr__(self, "created_at", _normalize_iso_datetime("callback subscription", "created_at", self.created_at))
        object.__setattr__(self, "expires_at", _optional_iso_datetime("callback subscription", "expires_at", self.expires_at))
        object.__setattr__(
            self,
            "replay_from_cursor",
            _optional_non_empty_string("callback subscription", "replay_from_cursor", self.replay_from_cursor),
        )
        if self.expires_at is not None:
            created_at = _parse_iso_datetime("callback subscription", "created_at", self.created_at)
            expires_at = _parse_iso_datetime("callback subscription", "expires_at", self.expires_at)
            if expires_at <= created_at:
                raise ValueError("callback subscription expires_at must be after created_at")

    def to_json(self) -> dict[str, object]:
        return {
            "subscription_id": self.subscription_id,
            "owner": self.owner,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "event_filter": self.event_filter.to_json(),
            "delivery_target": self.delivery_target,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "replay_from_cursor": self.replay_from_cursor,
            "failure_policy": self.failure_policy,
        }


@dataclass(frozen=True, slots=True)
class CallbackDelivery:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    sequence: int
    cursor: str
    attempt: int
    idempotency_key: str
    status: CallbackDeliveryStatus
    next_retry_at: str | None = None
    delivered_at: str | None = None
    acknowledged_at: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("delivery_id", "subscription_id", "event_id", "run_id", "cursor", "idempotency_key"):
            object.__setattr__(
                self,
                field_name,
                _validate_non_empty_string("callback delivery", field_name, getattr(self, field_name)),
            )
        object.__setattr__(self, "sequence", _validate_non_negative_int("callback delivery", "sequence", self.sequence))
        object.__setattr__(self, "attempt", _validate_positive_int("callback delivery", "attempt", self.attempt))
        status = _validate_non_empty_string("callback delivery", "status", self.status)
        if status not in VALID_CALLBACK_DELIVERY_STATUSES:
            raise ValueError("callback delivery status must be a valid callback delivery status")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "next_retry_at", _optional_iso_datetime("callback delivery", "next_retry_at", self.next_retry_at))
        object.__setattr__(self, "delivered_at", _optional_iso_datetime("callback delivery", "delivered_at", self.delivered_at))
        object.__setattr__(
            self,
            "acknowledged_at",
            _optional_iso_datetime("callback delivery", "acknowledged_at", self.acknowledged_at),
        )
        object.__setattr__(self, "last_error", _optional_non_empty_string("callback delivery", "last_error", self.last_error))
        if self.status != "acknowledged" and self.acknowledged_at is not None:
            raise ValueError("callback delivery acknowledged_at requires acknowledged status")
        if self.status in {"failed", "dead_lettered", "cancelled", "expired"} and self.last_error is None:
            raise ValueError("terminal failure callback delivery requires last_error")
        if self.status in TERMINAL_CALLBACK_DELIVERY_STATUSES and self.next_retry_at is not None:
            raise ValueError("terminal callback delivery must not have next_retry_at")
        if self.status == "delivered" and self.delivered_at is None:
            raise ValueError("delivered callback delivery requires delivered_at")
        if self.status == "acknowledged":
            if self.delivered_at is None:
                raise ValueError("acknowledged callback delivery requires delivered_at")
            if self.acknowledged_at is None:
                raise ValueError("acknowledged callback delivery requires acknowledged_at")
            delivered_at = _parse_iso_datetime("callback delivery", "delivered_at", self.delivered_at)
            acknowledged_at = _parse_iso_datetime("callback delivery", "acknowledged_at", self.acknowledged_at)
            if acknowledged_at < delivered_at:
                raise ValueError("acknowledged callback delivery must not precede delivered_at")
        if self.status in {"pending", "delivering"} and self.delivered_at is not None:
            raise ValueError("pending callback delivery must not already have delivered_at")

    def to_json(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "subscription_id": self.subscription_id,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "cursor": self.cursor,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "next_retry_at": self.next_retry_at,
            "delivered_at": self.delivered_at,
            "acknowledged_at": self.acknowledged_at,
            "last_error": self.last_error,
        }


from .webhooks import *  # noqa: E402,F403
from .webhooks import __all__ as _webhook_exports  # noqa: E402


__all__ = [
    "CallbackDelivery",
    "CallbackSubscription",
    "EventFilter",
    *_webhook_exports,
]
