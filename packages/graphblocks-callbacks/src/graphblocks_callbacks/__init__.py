from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
import json
import math
from urllib.parse import urlparse

from graphblocks import ArtifactRef, canonical_dumps, canonical_hash


REQUIRED_WEBHOOK_HEADERS = (
    "GraphBlocks-Delivery-Id",
    "GraphBlocks-Event-Id",
    "GraphBlocks-Run-Id",
    "GraphBlocks-Cursor",
    "GraphBlocks-Idempotency-Key",
    "GraphBlocks-Timestamp",
    "GraphBlocks-Signature",
    "GraphBlocks-Signature-Algorithm",
)
VALID_DELIVERY_STATUSES = frozenset({
    "pending",
    "delivering",
    "delivered",
    "acknowledged",
    "failed",
    "dead_lettered",
    "cancelled",
    "expired",
})
TERMINAL_DELIVERY_STATUSES = frozenset({
    "delivered",
    "acknowledged",
    "dead_lettered",
    "cancelled",
    "expired",
})
TERMINAL_FAILURE_DELIVERY_STATUSES = frozenset({"failed", "dead_lettered", "cancelled", "expired"})
VALID_CALLBACK_FAILURE_POLICIES = frozenset({
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
})
VALID_CALLBACK_AUTH_KINDS = frozenset({"bearer", "hmac", "mtls", "oidc"})
FORBIDDEN_WEBHOOK_HOSTS = frozenset({"localhost", "metadata.google.internal"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    _require_non_empty_string("timestamp", value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be an ISO-8601 datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_field_timestamp(field_name: str, value: str) -> datetime:
    _require_non_empty_string(field_name, value)
    try:
        return _parse_utc_timestamp(value)
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from None


def _require_non_empty_string(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _non_negative_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _validate_http_status_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        raise ValueError("status_code must be a valid HTTP status")
    return value


def _string_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise ValueError("headers must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("headers keys must be non-empty strings")
        if not isinstance(value, str):
            raise ValueError("headers values must be strings")
        normalized_key = key.lower()
        if normalized_key in normalized:
            raise ValueError("headers must not contain duplicate case-insensitive keys")
        normalized[normalized_key] = value
    return normalized


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload must not contain non-finite numbers")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("payload must contain only string object keys")
            _validate_json_value(item)
        return
    raise ValueError("payload must contain only JSON values")


def _deterministic_jitter_ms(seed: str, jitter_ms: int) -> int:
    if jitter_ms == 0:
        return 0
    digest = canonical_hash({"seed": seed}).split(":", 1)[-1]
    return int(digest[:8], 16) % (jitter_ms + 1)


def _json_payload(value: Mapping[str, object]) -> dict[str, object]:
    _validate_json_value(dict(value))
    json.dumps(value, allow_nan=False)
    return deepcopy(dict(value))


def _callback_resume_binding_key(
    *,
    tenant_id: str | None,
    release_id: str,
    run_id: str,
    node_id: str,
    attempt_id: str,
    operation_id: str,
) -> str:
    return canonical_hash(
        {
            "tenant_id": "" if tenant_id is None else tenant_id,
            "release_id": release_id,
            "run_id": run_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "operation_id": operation_id,
        }
    )


def _retry_after_timestamp(headers: Mapping[str, str] | None, received_at: str | None) -> str | None:
    retry_after = _string_headers(headers).get("retry-after")
    if retry_after is None or not retry_after.strip():
        return None
    retry_after = retry_after.strip()
    if retry_after.isdecimal():
        seconds = _non_negative_int("Retry-After", int(retry_after))
        received = _parse_utc_timestamp(_utc_now_iso() if received_at is None else received_at)
        return _format_utc_timestamp(received + timedelta(seconds=seconds))
    try:
        retry_at = _parse_utc_timestamp(retry_after)
    except ValueError:
        return None
    received = _parse_utc_timestamp(_utc_now_iso() if received_at is None else received_at)
    if retry_at <= received:
        return None
    return _format_utc_timestamp(retry_at)


@dataclass(frozen=True, slots=True)
class WebhookResponseDecision:
    status_code: int
    status: str
    retry: bool
    terminal: bool
    reason: str
    retry_after: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_code", _validate_http_status_code(self.status_code))
        _require_non_empty_string("status", self.status)
        if not isinstance(self.retry, bool):
            raise ValueError("retry must be a boolean")
        if not isinstance(self.terminal, bool):
            raise ValueError("terminal must be a boolean")
        _require_non_empty_string("reason", self.reason)
        if self.retry_after is not None:
            _require_non_empty_string("retry_after", self.retry_after)


def classify_webhook_response(
    status_code: int,
    *,
    headers: Mapping[str, str] | None = None,
    received_at: str | None = None,
) -> WebhookResponseDecision:
    status_code = _validate_http_status_code(status_code)
    normalized_headers = _string_headers(headers)
    if received_at is not None:
        _require_non_empty_string("received_at", received_at)

    if 200 <= status_code <= 299:
        return WebhookResponseDecision(status_code, "delivered", retry=False, terminal=True, reason="accepted")
    if status_code == 409:
        return WebhookResponseDecision(
            status_code,
            "acknowledged",
            retry=False,
            terminal=True,
            reason="duplicate_already_processed",
        )
    if status_code == 410:
        return WebhookResponseDecision(status_code, "gone", retry=False, terminal=True, reason="subscription_gone")
    if status_code == 429:
        return WebhookResponseDecision(
            status_code,
            "retry",
            retry=True,
            terminal=False,
            reason="rate_limited",
            retry_after=_retry_after_timestamp(normalized_headers, received_at),
        )
    if 500 <= status_code <= 599:
        return WebhookResponseDecision(status_code, "retry", retry=True, terminal=False, reason="receiver_error")
    return WebhookResponseDecision(status_code, "failed", retry=False, terminal=True, reason="non_retryable")


@dataclass(frozen=True, slots=True)
class CallbackPayloadProjection:
    mode: str
    payload: dict[str, object] = field(default_factory=dict)
    payload_digest: str | None = None
    payload_size_bytes: int = 0
    artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string("mode", self.mode)
        if self.mode not in {"inline", "artifact_reference"}:
            raise ValueError("mode must be inline or artifact_reference")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a JSON object")
        object.__setattr__(self, "payload", _json_payload(self.payload))
        if self.payload_digest is None:
            raise ValueError("callback payload projection requires payload_digest")
        _require_non_empty_string("payload_digest", self.payload_digest)
        object.__setattr__(self, "payload_size_bytes", _non_negative_int("payload_size_bytes", self.payload_size_bytes))
        if self.mode == "inline":
            canonical = canonical_dumps(self.payload).encode("utf-8")
            expected_digest = canonical_hash(self.payload)
            if self.payload_digest is not None and self.payload_digest != expected_digest:
                raise ValueError("inline callback payload_digest must match payload")
            if self.payload_size_bytes != len(canonical):
                raise ValueError("inline callback payload_size_bytes must match canonical payload size")
        if self.mode == "inline" and self.artifact is not None:
            raise ValueError("inline callback payload projection must not include an artifact")
        if self.mode == "artifact_reference" and not isinstance(self.artifact, ArtifactRef):
            raise ValueError("artifact_reference callback payload projection requires an ArtifactRef")


def project_callback_payload(
    payload: Mapping[str, object],
    *,
    max_inline_bytes: int,
    artifact: ArtifactRef | None = None,
) -> CallbackPayloadProjection:
    max_inline_bytes = _non_negative_int("max_inline_bytes", max_inline_bytes)
    payload_copy = _json_payload(payload)
    canonical = canonical_dumps(payload_copy).encode("utf-8")
    digest = canonical_hash(payload_copy)
    if len(canonical) <= max_inline_bytes:
        return CallbackPayloadProjection(
            mode="inline",
            payload=payload_copy,
            payload_digest=digest,
            payload_size_bytes=len(canonical),
        )
    if artifact is None:
        raise ValueError("oversized callback payload requires an ArtifactRef")
    return CallbackPayloadProjection(
        mode="artifact_reference",
        payload={},
        payload_digest=digest,
        payload_size_bytes=len(canonical),
        artifact=artifact,
    )


def _host_is_forbidden(host: str) -> bool:
    normalized = host.strip().rstrip(".").lower()
    return normalized in FORBIDDEN_WEBHOOK_HOSTS or normalized.endswith(".localhost")


def _ip_is_forbidden(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


@dataclass(frozen=True, slots=True)
class WebhookTargetSafety:
    url: str
    allowed: bool
    reason: str
    host: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string("url", self.url)
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a boolean")
        _require_non_empty_string("reason", self.reason)
        if self.host is not None:
            _require_non_empty_string("host", self.host)


def validate_webhook_target_url(url: str, *, allow_private: bool = False) -> WebhookTargetSafety:
    _require_non_empty_string("url", url)
    if not isinstance(allow_private, bool):
        raise ValueError("allow_private must be a boolean")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return WebhookTargetSafety(url=url, allowed=False, reason="unsupported_scheme", host=parsed.hostname)
    if parsed.username is not None or parsed.password is not None:
        return WebhookTargetSafety(url=url, allowed=False, reason="userinfo_not_allowed", host=parsed.hostname)
    if parsed.hostname is None or not parsed.hostname.strip():
        return WebhookTargetSafety(url=url, allowed=False, reason="missing_host", host=None)

    host = parsed.hostname.strip().rstrip(".").lower()
    if not allow_private and _host_is_forbidden(host):
        return WebhookTargetSafety(url=url, allowed=False, reason="forbidden_host", host=host)
    if not allow_private and _ip_is_forbidden(host):
        return WebhookTargetSafety(url=url, allowed=False, reason="forbidden_ip", host=host)
    return WebhookTargetSafety(url=url, allowed=True, reason="allowed", host=host)


@dataclass(frozen=True, slots=True)
class CallbackRetryPolicy:
    max_attempts: int = 8
    initial_delay_ms: int = 500
    max_delay_ms: int = 30_000
    jitter_ms: int = 250

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", _positive_int("max_attempts", self.max_attempts))
        object.__setattr__(
            self,
            "initial_delay_ms",
            _non_negative_int("initial_delay_ms", self.initial_delay_ms),
        )
        object.__setattr__(self, "max_delay_ms", _non_negative_int("max_delay_ms", self.max_delay_ms))
        object.__setattr__(self, "jitter_ms", _non_negative_int("jitter_ms", self.jitter_ms))
        if self.initial_delay_ms > self.max_delay_ms:
            raise ValueError("initial_delay_ms must be less than or equal to max_delay_ms")

    def delay_ms(self, *, delivery_id: str, attempt: int) -> int:
        _require_non_empty_string("delivery_id", delivery_id)
        attempt = _positive_int("attempt", attempt)
        base = min(self.max_delay_ms, self.initial_delay_ms * (2 ** max(0, attempt - 1)))
        jitter = _deterministic_jitter_ms(f"{delivery_id}:{attempt}", self.jitter_ms)
        return min(self.max_delay_ms, base + jitter)


@dataclass(frozen=True, slots=True)
class CallbackDeliveryProjection:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    sequence: int
    cursor: str
    attempt: int
    idempotency_key: str
    status: str = "pending"
    next_retry_at: str | None = None
    delivered_at: str | None = None
    acknowledged_at: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("delivery_id", "subscription_id", "event_id", "run_id", "cursor", "idempotency_key"):
            _require_non_empty_string(field_name, getattr(self, field_name))
        object.__setattr__(self, "sequence", _non_negative_int("sequence", self.sequence))
        object.__setattr__(self, "attempt", _positive_int("attempt", self.attempt))
        _require_non_empty_string("status", self.status)
        if self.status not in VALID_DELIVERY_STATUSES:
            raise ValueError("status must be a valid callback delivery status")
        parsed_timestamps: dict[str, datetime] = {}
        for field_name in ("next_retry_at", "delivered_at", "acknowledged_at", "last_error"):
            value = getattr(self, field_name)
            if value is not None:
                if field_name == "last_error":
                    _require_non_empty_string(field_name, value)
                else:
                    parsed_timestamps[field_name] = _parse_field_timestamp(field_name, value)
        delivered_at = parsed_timestamps.get("delivered_at")
        acknowledged_at = parsed_timestamps.get("acknowledged_at")
        if delivered_at is not None and acknowledged_at is not None and acknowledged_at < delivered_at:
            raise ValueError("acknowledged_at must not be before delivered_at")

    def mark_failed(self, error: str) -> CallbackDeliveryProjection:
        _require_non_empty_string("error", error)
        return CallbackDeliveryProjection(
            delivery_id=self.delivery_id,
            subscription_id=self.subscription_id,
            event_id=self.event_id,
            run_id=self.run_id,
            sequence=self.sequence,
            cursor=self.cursor,
            attempt=self.attempt,
            idempotency_key=self.idempotency_key,
            status="failed",
            next_retry_at=None,
            delivered_at=self.delivered_at,
            acknowledged_at=self.acknowledged_at,
            last_error=error,
        )

    def schedule_retry(
        self,
        policy: CallbackRetryPolicy,
        *,
        failed_at: str,
        error: str,
    ) -> CallbackDeliveryProjection:
        if not isinstance(policy, CallbackRetryPolicy):
            raise ValueError("policy must be a CallbackRetryPolicy")
        if self.status == "pending" and self.next_retry_at is not None:
            return self
        if self.attempt >= policy.max_attempts:
            return self
        _require_non_empty_string("error", error)
        retry_attempt = self.attempt + 1
        retry_at = _parse_utc_timestamp(failed_at) + timedelta(
            milliseconds=policy.delay_ms(delivery_id=self.delivery_id, attempt=retry_attempt)
        )
        return CallbackDeliveryProjection(
            delivery_id=self.delivery_id,
            subscription_id=self.subscription_id,
            event_id=self.event_id,
            run_id=self.run_id,
            sequence=self.sequence,
            cursor=self.cursor,
            attempt=retry_attempt,
            idempotency_key=self.idempotency_key,
            status="pending",
            next_retry_at=_format_utc_timestamp(retry_at),
            delivered_at=self.delivered_at,
            acknowledged_at=self.acknowledged_at,
            last_error=error,
        )

    def apply_webhook_response(
        self,
        decision: WebhookResponseDecision,
        *,
        received_at: str,
        policy: CallbackRetryPolicy,
    ) -> CallbackDeliveryProjection:
        if not isinstance(decision, WebhookResponseDecision):
            raise ValueError("decision must be a WebhookResponseDecision")
        if not isinstance(policy, CallbackRetryPolicy):
            raise ValueError("policy must be a CallbackRetryPolicy")
        _require_non_empty_string("received_at", received_at)
        if self.status in TERMINAL_DELIVERY_STATUSES:
            raise ValueError("terminal callback delivery cannot apply webhook response")

        if decision.status == "delivered":
            return CallbackDeliveryProjection(
                delivery_id=self.delivery_id,
                subscription_id=self.subscription_id,
                event_id=self.event_id,
                run_id=self.run_id,
                sequence=self.sequence,
                cursor=self.cursor,
                attempt=self.attempt,
                idempotency_key=self.idempotency_key,
                status="delivered",
                delivered_at=received_at,
            )
        if decision.status == "acknowledged":
            return CallbackDeliveryProjection(
                delivery_id=self.delivery_id,
                subscription_id=self.subscription_id,
                event_id=self.event_id,
                run_id=self.run_id,
                sequence=self.sequence,
                cursor=self.cursor,
                attempt=self.attempt,
                idempotency_key=self.idempotency_key,
                status="acknowledged",
                delivered_at=received_at,
                acknowledged_at=received_at,
            )
        if decision.status == "gone":
            return CallbackDeliveryProjection(
                delivery_id=self.delivery_id,
                subscription_id=self.subscription_id,
                event_id=self.event_id,
                run_id=self.run_id,
                sequence=self.sequence,
                cursor=self.cursor,
                attempt=self.attempt,
                idempotency_key=self.idempotency_key,
                status="cancelled",
                delivered_at=received_at,
                last_error=decision.reason,
            )
        if decision.retry:
            failed = self.mark_failed(decision.reason)
            if self.attempt >= policy.max_attempts:
                return CallbackDeliveryProjection(
                    delivery_id=failed.delivery_id,
                    subscription_id=failed.subscription_id,
                    event_id=failed.event_id,
                    run_id=failed.run_id,
                    sequence=failed.sequence,
                    cursor=failed.cursor,
                    attempt=failed.attempt,
                    idempotency_key=failed.idempotency_key,
                    status="failed",
                    delivered_at=received_at,
                    last_error=decision.reason,
                )
            if decision.retry_after is not None:
                received = _parse_utc_timestamp(received_at)
                retry_after = _parse_utc_timestamp(decision.retry_after)
                retry_cap = received + timedelta(milliseconds=policy.max_delay_ms)
                if retry_after > retry_cap:
                    retry_after = retry_cap
                return CallbackDeliveryProjection(
                    delivery_id=self.delivery_id,
                    subscription_id=self.subscription_id,
                    event_id=self.event_id,
                    run_id=self.run_id,
                    sequence=self.sequence,
                    cursor=self.cursor,
                    attempt=self.attempt + 1,
                    idempotency_key=self.idempotency_key,
                    status="pending",
                    next_retry_at=_format_utc_timestamp(retry_after),
                    delivered_at=self.delivered_at,
                    acknowledged_at=self.acknowledged_at,
                    last_error=decision.reason,
                )
            return failed.schedule_retry(policy, failed_at=received_at, error=decision.reason)
        return CallbackDeliveryProjection(
            delivery_id=self.delivery_id,
            subscription_id=self.subscription_id,
            event_id=self.event_id,
            run_id=self.run_id,
            sequence=self.sequence,
            cursor=self.cursor,
            attempt=self.attempt,
            idempotency_key=self.idempotency_key,
            status="failed",
            delivered_at=received_at,
            last_error=decision.reason,
        )

    def to_dead_letter(
        self,
        policy: CallbackRetryPolicy,
        *,
        dead_lettered_at: str,
        reason: str,
    ) -> CallbackDeadLetterRecord:
        if not isinstance(policy, CallbackRetryPolicy):
            raise ValueError("policy must be a CallbackRetryPolicy")
        _require_non_empty_string("dead_lettered_at", dead_lettered_at)
        _require_non_empty_string("reason", reason)
        parsed_dead_lettered_at = _parse_field_timestamp("dead_lettered_at", dead_lettered_at)
        if self.delivered_at is not None and parsed_dead_lettered_at < _parse_field_timestamp(
            "delivery delivered_at",
            self.delivered_at,
        ):
            raise ValueError("dead_lettered_at must not be before delivery delivered_at")
        attempt_history = tuple(range(1, self.attempt + 1))
        return CallbackDeadLetterRecord(
            delivery=CallbackDeliveryProjection(
                delivery_id=self.delivery_id,
                subscription_id=self.subscription_id,
                event_id=self.event_id,
                run_id=self.run_id,
                sequence=self.sequence,
                cursor=self.cursor,
                attempt=self.attempt,
                idempotency_key=self.idempotency_key,
                status="dead_lettered",
                next_retry_at=None,
                delivered_at=self.delivered_at,
                acknowledged_at=self.acknowledged_at,
                last_error=self.last_error,
            ),
            attempt_history=attempt_history,
            dead_lettered_at=dead_lettered_at,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class CallbackDeliveryFailureAction:
    action: str
    run_id: str
    delivery_id: str
    reason: str
    terminal_delivery: bool

    def __post_init__(self) -> None:
        _require_non_empty_string("action", self.action)
        if self.action not in {"none", "pause_run", "fail_run"}:
            raise ValueError("action must be none, pause_run, or fail_run")
        _require_non_empty_string("run_id", self.run_id)
        _require_non_empty_string("delivery_id", self.delivery_id)
        _require_non_empty_string("reason", self.reason)
        if not isinstance(self.terminal_delivery, bool):
            raise ValueError("terminal_delivery must be a boolean")


def evaluate_callback_delivery_failure_action(
    delivery: CallbackDeliveryProjection,
    failure_policy: str,
) -> CallbackDeliveryFailureAction:
    if not isinstance(delivery, CallbackDeliveryProjection):
        raise ValueError("delivery must be a CallbackDeliveryProjection")
    _require_non_empty_string("failure_policy", failure_policy)
    if failure_policy not in VALID_CALLBACK_FAILURE_POLICIES:
        raise ValueError(
            "failure_policy must be best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure"
        )
    reason = delivery.last_error or delivery.status
    if delivery.status not in TERMINAL_FAILURE_DELIVERY_STATUSES:
        return CallbackDeliveryFailureAction(
            action="none",
            run_id=delivery.run_id,
            delivery_id=delivery.delivery_id,
            reason="delivery_not_terminal",
            terminal_delivery=False,
        )
    if failure_policy == "pause_run_on_failure":
        action = "pause_run"
    elif failure_policy == "fail_run_on_failure":
        action = "fail_run"
    else:
        action = "none"
    return CallbackDeliveryFailureAction(
        action=action,
        run_id=delivery.run_id,
        delivery_id=delivery.delivery_id,
        reason=reason,
        terminal_delivery=True,
    )


@dataclass(frozen=True, slots=True)
class CallbackRedriveRecord:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    sequence: int
    cursor: str
    idempotency_key: str
    attempt_history: tuple[int, ...]
    operator_principal: str
    reason: str
    redriven_at: str

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_id",
            "subscription_id",
            "event_id",
            "run_id",
            "cursor",
            "idempotency_key",
            "operator_principal",
            "reason",
            "redriven_at",
        ):
            _require_non_empty_string(field_name, getattr(self, field_name))
        object.__setattr__(self, "sequence", _non_negative_int("sequence", self.sequence))
        object.__setattr__(
            self,
            "attempt_history",
            tuple(_positive_int("attempt_history item", item) for item in self.attempt_history),
        )


@dataclass(frozen=True, slots=True)
class CallbackDeadLetterRecord:
    delivery: CallbackDeliveryProjection
    attempt_history: tuple[int, ...]
    dead_lettered_at: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, CallbackDeliveryProjection):
            raise ValueError("delivery must be a CallbackDeliveryProjection")
        if self.delivery.status != "dead_lettered":
            raise ValueError("dead-letter record delivery must have dead_lettered status")
        object.__setattr__(
            self,
            "attempt_history",
            tuple(_positive_int("attempt_history item", item) for item in self.attempt_history),
        )
        if self.delivery.attempt not in self.attempt_history:
            raise ValueError("dead-letter record attempt_history must include delivery attempt")
        parsed_dead_lettered_at = _parse_field_timestamp("dead_lettered_at", self.dead_lettered_at)
        if self.delivery.delivered_at is not None and parsed_dead_lettered_at < _parse_field_timestamp(
            "delivery delivered_at",
            self.delivery.delivered_at,
        ):
            raise ValueError("dead_lettered_at must not be before delivery delivered_at")
        _require_non_empty_string("reason", self.reason)

    def redrive(
        self,
        *,
        operator_principal: str,
        reason: str,
        redriven_at: str,
    ) -> CallbackRedriveRecord:
        if _parse_field_timestamp("redriven_at", redriven_at) < _parse_field_timestamp(
            "dead_lettered_at",
            self.dead_lettered_at,
        ):
            raise ValueError("redriven_at must not be before dead_lettered_at")
        return CallbackRedriveRecord(
            delivery_id=self.delivery.delivery_id,
            subscription_id=self.delivery.subscription_id,
            event_id=self.delivery.event_id,
            run_id=self.delivery.run_id,
            sequence=self.delivery.sequence,
            cursor=self.delivery.cursor,
            idempotency_key=self.delivery.idempotency_key,
            attempt_history=self.attempt_history,
            operator_principal=operator_principal,
            reason=reason,
            redriven_at=redriven_at,
        )

    def redrive_delivery(
        self,
        *,
        redriven_at: str,
        reason: str,
    ) -> CallbackDeliveryProjection:
        if _parse_field_timestamp("redriven_at", redriven_at) < _parse_field_timestamp(
            "dead_lettered_at",
            self.dead_lettered_at,
        ):
            raise ValueError("redriven_at must not be before dead_lettered_at")
        _require_non_empty_string("reason", reason)
        next_attempt = max(self.attempt_history, default=self.delivery.attempt) + 1
        return CallbackDeliveryProjection(
            delivery_id=self.delivery.delivery_id,
            subscription_id=self.delivery.subscription_id,
            event_id=self.delivery.event_id,
            run_id=self.delivery.run_id,
            sequence=self.delivery.sequence,
            cursor=self.delivery.cursor,
            attempt=next_attempt,
            idempotency_key=self.delivery.idempotency_key,
            status="pending",
            next_retry_at=redriven_at,
            delivered_at=None,
            acknowledged_at=None,
            last_error=reason,
        )


@dataclass(frozen=True, slots=True)
class CallbackReplayRecord:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    cursor: str
    idempotency_key: str
    envelope_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_id",
            "subscription_id",
            "event_id",
            "run_id",
            "cursor",
            "idempotency_key",
            "envelope_digest",
        ):
            _require_non_empty_string(field_name, getattr(self, field_name))


@dataclass(frozen=True, slots=True)
class CallbackReplayDecision:
    status: str
    replay_record: CallbackReplayRecord
    incoming_digest: str
    duplicate: bool
    conflict: bool

    def __post_init__(self) -> None:
        _require_non_empty_string("status", self.status)
        if self.status not in {"accepted", "duplicate", "conflict"}:
            raise ValueError("status must be accepted, duplicate, or conflict")
        if not isinstance(self.replay_record, CallbackReplayRecord):
            raise ValueError("replay_record must be a CallbackReplayRecord")
        _require_non_empty_string("incoming_digest", self.incoming_digest)
        if not isinstance(self.duplicate, bool):
            raise ValueError("duplicate must be a boolean")
        if not isinstance(self.conflict, bool):
            raise ValueError("conflict must be a boolean")


class CallbackReplayGuard:
    def __init__(self, records: Mapping[str, CallbackReplayRecord] | None = None) -> None:
        self._records_by_delivery_id: dict[str, CallbackReplayRecord] = {}
        self._records_by_subscription_event: dict[tuple[str, str], CallbackReplayRecord] = {}
        if records is None:
            self._records: dict[str, CallbackReplayRecord] = {}
            return
        if not isinstance(records, Mapping):
            raise ValueError("records must be a mapping")
        self._records = {}
        for key, record in records.items():
            _require_non_empty_string("record key", key)
            if not isinstance(record, CallbackReplayRecord):
                raise ValueError("records values must be CallbackReplayRecord")
            self._records[key] = record
            existing_delivery = self._records_by_delivery_id.get(record.delivery_id)
            if existing_delivery is not None and existing_delivery != record:
                raise ValueError("records contain conflicting delivery_id identity")
            existing_subscription_event = self._records_by_subscription_event.get(
                (record.subscription_id, record.event_id)
            )
            if existing_subscription_event is not None and existing_subscription_event != record:
                raise ValueError("records contain conflicting subscription/event identity")
            self._records_by_delivery_id[record.delivery_id] = record
            self._records_by_subscription_event[(record.subscription_id, record.event_id)] = record

    def record(self, envelope: CallbackEnvelope) -> CallbackReplayDecision:
        if not isinstance(envelope, CallbackEnvelope):
            raise ValueError("envelope must be a CallbackEnvelope")
        digest = envelope.payload_digest()
        existing = (
            self._records.get(envelope.idempotency_key)
            or self._records_by_delivery_id.get(envelope.delivery_id)
            or self._records_by_subscription_event.get((envelope.subscription_id, envelope.event_id))
        )
        if existing is None:
            record = CallbackReplayRecord(
                delivery_id=envelope.delivery_id,
                subscription_id=envelope.subscription_id,
                event_id=envelope.event_id,
                run_id=envelope.run_id,
                cursor=envelope.cursor,
                idempotency_key=envelope.idempotency_key,
                envelope_digest=digest,
            )
            self._records[envelope.idempotency_key] = record
            self._records_by_delivery_id[envelope.delivery_id] = record
            self._records_by_subscription_event[(envelope.subscription_id, envelope.event_id)] = record
            return CallbackReplayDecision(
                status="accepted",
                replay_record=record,
                incoming_digest=digest,
                duplicate=False,
                conflict=False,
            )
        if existing.envelope_digest == digest and existing.idempotency_key == envelope.idempotency_key:
            return CallbackReplayDecision(
                status="duplicate",
                replay_record=existing,
                incoming_digest=digest,
                duplicate=True,
                conflict=False,
            )
        return CallbackReplayDecision(
            status="conflict",
            replay_record=existing,
            incoming_digest=digest,
            duplicate=False,
            conflict=True,
        )

    def records(self) -> tuple[CallbackReplayRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


@dataclass(frozen=True, slots=True)
class CallbackEndpointAuth:
    kind: str
    token_ref: str | None = None
    secret_ref: str | None = None
    client_identity_ref: str | None = None
    issuer: str | None = None
    audience: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string("kind", self.kind)
        if self.kind not in VALID_CALLBACK_AUTH_KINDS:
            raise ValueError("kind must be bearer, hmac, mtls, or oidc")
        for field_name in ("token_ref", "secret_ref", "client_identity_ref", "issuer", "audience"):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_empty_string(field_name, value)
        if self.kind == "bearer" and self.token_ref is None:
            raise ValueError("bearer callback auth requires token_ref")
        if self.kind == "hmac" and self.secret_ref is None:
            raise ValueError("hmac callback auth requires secret_ref")
        if self.kind == "mtls" and self.client_identity_ref is None:
            raise ValueError("mtls callback auth requires client_identity_ref")
        if self.kind == "oidc" and (self.issuer is None or self.audience is None):
            raise ValueError("oidc callback auth requires issuer and audience")


@dataclass(frozen=True, slots=True)
class CallbackEndpointRef:
    endpoint_id: str
    url: str
    accepted_schema: str
    auth: CallbackEndpointAuth
    operation_id: str
    run_id: str
    node_id: str
    attempt_id: str
    release_id: str
    tenant_id: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "endpoint_id",
            "url",
            "accepted_schema",
            "operation_id",
            "run_id",
            "node_id",
            "attempt_id",
            "release_id",
            "tenant_id",
        ):
            _require_non_empty_string(field_name, getattr(self, field_name))
        if not isinstance(self.auth, CallbackEndpointAuth):
            raise ValueError("auth must be a CallbackEndpointAuth")
        if self.expires_at is not None:
            _parse_field_timestamp("expires_at", self.expires_at)

    def binding_key(self) -> str:
        return _callback_resume_binding_key(
            tenant_id=self.tenant_id,
            release_id=self.release_id,
            run_id=self.run_id,
            node_id=self.node_id,
            attempt_id=self.attempt_id,
            operation_id=self.operation_id,
        )


@dataclass(frozen=True, slots=True)
class CallbackEnvelope:
    delivery_id: str
    subscription_id: str
    event_id: str
    run_id: str
    sequence: int
    cursor: str
    type: str
    payload: dict[str, object]
    idempotency_key: str
    occurred_at: str
    delivered_at: str = field(default_factory=_utc_now_iso)
    release_id: str = "local"
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_id",
            "subscription_id",
            "event_id",
            "run_id",
            "cursor",
            "type",
            "idempotency_key",
            "occurred_at",
            "delivered_at",
            "release_id",
        ):
            _require_non_empty_string(field_name, getattr(self, field_name))
        if self.tenant_id is not None:
            _require_non_empty_string("tenant_id", self.tenant_id)
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        occurred_at = _parse_field_timestamp("occurred_at", self.occurred_at)
        delivered_at = _parse_field_timestamp("delivered_at", self.delivered_at)
        if delivered_at < occurred_at:
            raise ValueError("delivered_at must not be before occurred_at")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a JSON object")
        object.__setattr__(self, "payload", _json_payload(self.payload))

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "delivery_id": self.delivery_id,
            "subscription_id": self.subscription_id,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "cursor": self.cursor,
            "type": self.type,
            "payload": deepcopy(self.payload),
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
            "delivered_at": self.delivered_at,
            "release_id": self.release_id,
        }
        if self.tenant_id is not None:
            payload["tenant_id"] = self.tenant_id
        return payload

    def canonical_body(self) -> bytes:
        return canonical_dumps(self.to_payload()).encode("utf-8")

    def payload_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def unsigned_headers(self, *, timestamp: str | None = None) -> dict[str, str]:
        timestamp = self.delivered_at if timestamp is None else timestamp
        _parse_field_timestamp("timestamp", timestamp)
        return {
            "GraphBlocks-Delivery-Id": self.delivery_id,
            "GraphBlocks-Event-Id": self.event_id,
            "GraphBlocks-Run-Id": self.run_id,
            "GraphBlocks-Cursor": self.cursor,
            "GraphBlocks-Idempotency-Key": self.idempotency_key,
            "GraphBlocks-Timestamp": timestamp,
        }


@dataclass(frozen=True, slots=True)
class ExternalCallbackReceipt:
    callback_id: str
    operation_id: str
    run_id: str
    node_id: str
    attempt_id: str
    provider_operation_id: str | None
    idempotency_key: str
    payload_projection: CallbackPayloadProjection
    payload_digest: str
    received_at: str
    verified_by: str
    policy_snapshot_id: str
    release_id: str = "local"
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "callback_id",
            "operation_id",
            "run_id",
            "node_id",
            "attempt_id",
            "idempotency_key",
            "payload_digest",
            "received_at",
            "verified_by",
            "policy_snapshot_id",
            "release_id",
        ):
            _require_non_empty_string(field_name, getattr(self, field_name))
        if self.tenant_id is not None:
            _require_non_empty_string("tenant_id", self.tenant_id)
        if self.provider_operation_id is not None:
            _require_non_empty_string("provider_operation_id", self.provider_operation_id)
        _parse_field_timestamp("received_at", self.received_at)
        if not isinstance(self.payload_projection, CallbackPayloadProjection):
            raise ValueError("payload_projection must be a CallbackPayloadProjection")
        if self.payload_projection.payload_digest != self.payload_digest:
            raise ValueError("payload_digest must match the payload projection")

    def binding_key(self) -> str:
        return _callback_resume_binding_key(
            tenant_id=self.tenant_id,
            release_id=self.release_id,
            run_id=self.run_id,
            node_id=self.node_id,
            attempt_id=self.attempt_id,
            operation_id=self.operation_id,
        )


def record_external_callback_receipt(
    envelope: CallbackEnvelope,
    payload_projection: CallbackPayloadProjection,
    *,
    operation_id: str,
    run_id: str | None = None,
    node_id: str,
    attempt_id: str,
    release_id: str | None = None,
    tenant_id: str | None = None,
    verified_by: str,
    policy_snapshot_id: str,
    received_at: str,
    callback_id: str | None = None,
    provider_operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> ExternalCallbackReceipt:
    if not isinstance(envelope, CallbackEnvelope):
        raise ValueError("envelope must be a CallbackEnvelope")
    if not isinstance(payload_projection, CallbackPayloadProjection):
        raise ValueError("payload_projection must be a CallbackPayloadProjection")
    expected_payload_digest = canonical_hash(envelope.payload)
    if payload_projection.payload_digest != expected_payload_digest:
        raise ValueError("payload_projection must match the envelope payload")
    if _parse_field_timestamp("received_at", received_at) < _parse_field_timestamp(
        "envelope delivered_at",
        envelope.delivered_at,
    ):
        raise ValueError("received_at must not be before envelope delivered_at")
    if run_id is not None:
        _require_non_empty_string("run_id", run_id)
        if run_id != envelope.run_id:
            raise ValueError("run_id must match the envelope")
    if release_id is not None:
        _require_non_empty_string("release_id", release_id)
        if release_id != envelope.release_id:
            raise ValueError("release_id must match the envelope")
    if tenant_id is not None:
        _require_non_empty_string("tenant_id", tenant_id)
        if tenant_id != envelope.tenant_id:
            raise ValueError("tenant_id must match the envelope")
    callback_id = envelope.delivery_id if callback_id is None else callback_id
    idempotency_key = envelope.idempotency_key if idempotency_key is None else idempotency_key
    if idempotency_key != envelope.idempotency_key:
        raise ValueError("idempotency_key must match the envelope")
    return ExternalCallbackReceipt(
        callback_id=callback_id,
        operation_id=operation_id,
        run_id=envelope.run_id,
        node_id=node_id,
        attempt_id=attempt_id,
        provider_operation_id=provider_operation_id,
        idempotency_key=idempotency_key,
        payload_projection=payload_projection,
        payload_digest=payload_projection.payload_digest or "",
        received_at=received_at,
        verified_by=verified_by,
        policy_snapshot_id=policy_snapshot_id,
        release_id=envelope.release_id,
        tenant_id=envelope.tenant_id,
    )


@dataclass(frozen=True, slots=True)
class CallbackResumeDecision:
    status: str
    can_resume: bool
    reason: str
    endpoint_binding_key: str
    receipt_binding_key: str

    def __post_init__(self) -> None:
        _require_non_empty_string("status", self.status)
        if self.status not in {"admitted", "expired", "stale"}:
            raise ValueError("status must be admitted, expired, or stale")
        if not isinstance(self.can_resume, bool):
            raise ValueError("can_resume must be a boolean")
        for field_name in ("reason", "endpoint_binding_key", "receipt_binding_key"):
            _require_non_empty_string(field_name, getattr(self, field_name))


def evaluate_callback_resume(
    endpoint: CallbackEndpointRef,
    receipt: ExternalCallbackReceipt,
    *,
    now: str,
) -> CallbackResumeDecision:
    if not isinstance(endpoint, CallbackEndpointRef):
        raise ValueError("endpoint must be a CallbackEndpointRef")
    if not isinstance(receipt, ExternalCallbackReceipt):
        raise ValueError("receipt must be an ExternalCallbackReceipt")
    _require_non_empty_string("now", now)

    endpoint_binding_key = endpoint.binding_key()
    receipt_binding_key = receipt.binding_key()
    if endpoint.expires_at is not None and _parse_utc_timestamp(now) > _parse_utc_timestamp(endpoint.expires_at):
        return CallbackResumeDecision(
            status="expired",
            can_resume=False,
            reason="callback_endpoint_expired",
            endpoint_binding_key=endpoint_binding_key,
            receipt_binding_key=receipt_binding_key,
        )
    if endpoint_binding_key != receipt_binding_key:
        return CallbackResumeDecision(
            status="stale",
            can_resume=False,
            reason="callback_binding_mismatch",
            endpoint_binding_key=endpoint_binding_key,
            receipt_binding_key=receipt_binding_key,
        )
    return CallbackResumeDecision(
        status="admitted",
        can_resume=True,
        reason="current_callback",
        endpoint_binding_key=endpoint_binding_key,
        receipt_binding_key=receipt_binding_key,
    )


def sign_webhook_hmac_sha256(envelope: CallbackEnvelope, secret: bytes, *, timestamp: str | None = None) -> str:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    timestamp = envelope.delivered_at if timestamp is None else timestamp
    _parse_field_timestamp("timestamp", timestamp)
    body = timestamp.encode("utf-8") + b"." + envelope.canonical_body()
    return hmac.digest(secret, body, "sha256").hex()


def webhook_headers_hmac_sha256(
    envelope: CallbackEnvelope,
    secret: bytes,
    *,
    timestamp: str | None = None,
    key_id: str | None = None,
) -> dict[str, str]:
    timestamp = envelope.delivered_at if timestamp is None else timestamp
    headers = envelope.unsigned_headers(timestamp=timestamp)
    headers["GraphBlocks-Signature"] = sign_webhook_hmac_sha256(
        envelope,
        secret,
        timestamp=timestamp,
    )
    headers["GraphBlocks-Signature-Algorithm"] = "hmac-sha256"
    if key_id is not None:
        _require_non_empty_string("key_id", key_id)
        headers["GraphBlocks-Key-Id"] = key_id
    return headers


def verify_webhook_hmac_sha256(
    envelope: CallbackEnvelope,
    secret: bytes,
    signature: str,
    *,
    timestamp: str | None = None,
) -> bool:
    _require_non_empty_string("signature", signature)
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    try:
        expected = sign_webhook_hmac_sha256(envelope, secret, timestamp=timestamp)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature)


def verify_webhook_headers_hmac_sha256(
    envelope: CallbackEnvelope,
    headers: Mapping[str, str],
    secret: bytes,
    *,
    now: str | None = None,
    replay_window_seconds: int = 300,
) -> bool:
    if (
        isinstance(replay_window_seconds, bool)
        or not isinstance(replay_window_seconds, int)
        or replay_window_seconds < 0
    ):
        raise ValueError("replay_window_seconds must be a non-negative integer")
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")

    try:
        normalized = _string_headers(headers)
    except ValueError:
        return False
    for header in REQUIRED_WEBHOOK_HEADERS:
        value = normalized.get(header.lower())
        if not isinstance(value, str) or not value.strip():
            return False

    try:
        expected = envelope.unsigned_headers(timestamp=normalized["graphblocks-timestamp"])
    except ValueError:
        return False
    for header, value in expected.items():
        if normalized.get(header.lower()) != value:
            return False
    if normalized["graphblocks-signature-algorithm"] != "hmac-sha256":
        return False

    try:
        delivered_at = _parse_utc_timestamp(normalized["graphblocks-timestamp"])
        reference = _parse_utc_timestamp(_utc_now_iso() if now is None else now)
    except ValueError:
        return False
    if abs((reference - delivered_at).total_seconds()) > replay_window_seconds:
        return False

    return verify_webhook_hmac_sha256(
        envelope,
        secret,
        normalized["graphblocks-signature"],
        timestamp=normalized["graphblocks-timestamp"],
    )


def verify_webhook_headers_hmac_sha256_keyring(
    envelope: CallbackEnvelope,
    headers: Mapping[str, str],
    secrets_by_key_id: Mapping[str, bytes],
    *,
    now: str | None = None,
    replay_window_seconds: int = 300,
) -> str | None:
    if (
        isinstance(replay_window_seconds, bool)
        or not isinstance(replay_window_seconds, int)
        or replay_window_seconds < 0
    ):
        raise ValueError("replay_window_seconds must be a non-negative integer")
    if not isinstance(secrets_by_key_id, Mapping):
        raise ValueError("secrets_by_key_id must be a mapping")
    if not secrets_by_key_id:
        raise ValueError("secrets_by_key_id must contain at least one key")
    configured: list[tuple[str, bytes]] = []
    for key_id, secret in secrets_by_key_id.items():
        _require_non_empty_string("key_id", key_id)
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("secret values must be non-empty bytes")
        configured.append((key_id, secret))
    try:
        normalized = _string_headers(headers)
    except ValueError:
        return None
    requested_key_id = normalized.get("graphblocks-key-id")
    candidates = [
        (key_id, secret)
        for key_id, secret in configured
        if requested_key_id is None or requested_key_id == key_id
    ]
    if not candidates:
        return None
    for key_id, secret in candidates:
        if verify_webhook_headers_hmac_sha256(
            envelope,
            headers,
            secret,
            now=now,
            replay_window_seconds=replay_window_seconds,
        ):
            return key_id
    return None


__all__ = [
    "CallbackDeadLetterRecord",
    "CallbackDeliveryFailureAction",
    "CallbackDeliveryProjection",
    "CallbackEndpointAuth",
    "CallbackEndpointRef",
    "CallbackEnvelope",
    "CallbackPayloadProjection",
    "CallbackRedriveRecord",
    "CallbackReplayDecision",
    "CallbackReplayGuard",
    "CallbackReplayRecord",
    "CallbackResumeDecision",
    "CallbackRetryPolicy",
    "ExternalCallbackReceipt",
    "REQUIRED_WEBHOOK_HEADERS",
    "WebhookTargetSafety",
    "WebhookResponseDecision",
    "classify_webhook_response",
    "evaluate_callback_delivery_failure_action",
    "evaluate_callback_resume",
    "project_callback_payload",
    "record_external_callback_receipt",
    "sign_webhook_hmac_sha256",
    "validate_webhook_target_url",
    "verify_webhook_headers_hmac_sha256",
    "verify_webhook_headers_hmac_sha256_keyring",
    "verify_webhook_hmac_sha256",
    "webhook_headers_hmac_sha256",
]
