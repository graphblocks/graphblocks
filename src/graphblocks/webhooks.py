from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hmac
import math
import socket
from threading import RLock
from typing import Protocol
from urllib.parse import urlparse

from .canonical import MAX_CANONICAL_JSON_DEPTH, canonical_dumps, canonical_hash
from .documents import ArtifactRef
from .server import ServerCallbackDeliveryResult, ServerCallbackRegistration
from .url_validation import validate_webhook_url


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
    "failed",
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


class _FrozenJsonArray(tuple[object, ...]):
    pass


class _FrozenJsonObject(dict[str, object]):
    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("frozen JSON object cannot be mutated")

    def __delitem__(self, key: str) -> None:
        raise TypeError("frozen JSON object cannot be mutated")

    def clear(self) -> None:
        raise TypeError("frozen JSON object cannot be mutated")

    def pop(self, key: str, default: object = None) -> object:
        raise TypeError("frozen JSON object cannot be mutated")

    def popitem(self) -> tuple[str, object]:
        raise TypeError("frozen JSON object cannot be mutated")

    def setdefault(self, key: str, default: object = None) -> object:
        raise TypeError("frozen JSON object cannot be mutated")

    def update(self, *args: object, **kwargs: object) -> None:
        raise TypeError("frozen JSON object cannot be mutated")

    def __ior__(self, other: object) -> _FrozenJsonObject:
        raise TypeError("frozen JSON object cannot be mutated")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    _require_non_empty_string("timestamp", value)
    timestamp = value.strip()
    if timestamp != value or len(timestamp) <= 10 or timestamp[10] != "T":
        raise ValueError("timestamp must be an ISO-8601 datetime")
    suffix = timestamp[19:]
    suffix_valid = False
    if suffix.startswith("."):
        offset_start = min(
            (
                position
                for position in (
                    suffix.find("Z"),
                    suffix.find("+"),
                    suffix.find("-"),
                )
                if position >= 0
            ),
            default=-1,
        )
        if offset_start > 1 and suffix[1:offset_start].isdigit():
            suffix = suffix[offset_start:]
    if suffix == "Z":
        suffix_valid = True
    elif (
        len(suffix) == 6
        and suffix[0] in "+-"
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
        and 0 <= int(suffix[1:3]) <= 23
        and 0 <= int(suffix[4:6]) <= 59
    ):
        suffix_valid = True
    if not suffix_valid:
        raise ValueError("timestamp must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00") if timestamp.endswith("Z") else timestamp
        )
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


def _require_stable_string(field_name: str, value: str) -> None:
    _require_non_empty_string(field_name, value)
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")


def _require_sha256_digest(field_name: str, value: str) -> None:
    _require_non_empty_string(field_name, value)
    if not value.startswith("sha256:"):
        raise ValueError(f"{field_name} must be a sha256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field_name} must be a sha256 digest")


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
        _require_stable_string("headers keys", key)
        if not isinstance(value, str):
            raise ValueError("headers values must be strings")
        normalized_key = key.lower()
        if normalized_key in normalized:
            raise ValueError("headers must not contain duplicate case-insensitive keys")
        normalized[normalized_key] = value
    return normalized


def _freeze_json_value(
    value: object,
    *,
    key_path: str = "payload",
    _depth: int = 0,
    _active_containers: set[int] | None = None,
) -> object:
    if _depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            f"payload nesting must not exceed {MAX_CANONICAL_JSON_DEPTH} levels"
        )
    if _active_containers is None:
        _active_containers = set()
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload must not contain non-finite numbers")
        return value
    if isinstance(value, _FrozenJsonArray):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError("payload must not be recursive")
        _active_containers.add(container_id)
        try:
            return _FrozenJsonArray(
                _freeze_json_value(
                    item,
                    key_path=key_path,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
                for item in value
            )
        finally:
            _active_containers.remove(container_id)
    if isinstance(value, list):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError("payload must not be recursive")
        _active_containers.add(container_id)
        try:
            return _FrozenJsonArray(
                _freeze_json_value(
                    item,
                    key_path=key_path,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
                for item in value
            )
        finally:
            _active_containers.remove(container_id)
    if isinstance(value, tuple):
        raise ValueError("payload must contain only JSON values")
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError("payload must not be recursive")
        _active_containers.add(container_id)
        frozen: dict[str, object] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("payload must contain only string object keys")
                if not key.strip():
                    raise ValueError(f"{key_path} keys must be non-empty strings")
                if key != key.strip():
                    raise ValueError(f"{key_path} keys must not contain surrounding whitespace")
                frozen[key] = _freeze_json_value(
                    item,
                    key_path=f"{key_path}.{key}",
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
            return _FrozenJsonObject(frozen)
        finally:
            _active_containers.remove(container_id)
    raise ValueError("payload must contain only JSON values")


def _thaw_json_value(value: object) -> object:
    if isinstance(value, _FrozenJsonArray):
        return [_thaw_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    return deepcopy(value)


def _deterministic_jitter_ms(seed: str, jitter_ms: int) -> int:
    if jitter_ms == 0:
        return 0
    digest = canonical_hash({"seed": seed}).split(":", 1)[-1]
    return int(digest[:8], 16) % (jitter_ms + 1)


def _json_payload(value: Mapping[str, object]) -> _FrozenJsonObject:
    frozen = _freeze_json_value(value)
    if not isinstance(frozen, _FrozenJsonObject):
        raise ValueError("payload must be a JSON object")
    canonical_dumps(frozen)
    return frozen


def _is_terminal_delivery(status: str, next_retry_at: str | None) -> bool:
    return status in TERMINAL_DELIVERY_STATUSES and not (status == "failed" and next_retry_at is not None)


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
        try:
            seconds = int(retry_after)
        except ValueError:
            return None
        received = _parse_utc_timestamp(_utc_now_iso() if received_at is None else received_at)
        try:
            retry_at = received + timedelta(seconds=seconds)
        except OverflowError:
            return None
        return _format_utc_timestamp(retry_at)
    try:
        retry_at = _parse_utc_timestamp(retry_after)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            return None
        retry_at = retry_at.astimezone(timezone.utc)
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
            _parse_field_timestamp("retry_after", self.retry_after)


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
    if status_code in {408, 425}:
        return WebhookResponseDecision(
            status_code,
            "retry",
            retry=True,
            terminal=False,
            reason="receiver_not_ready",
        )
    if 500 <= status_code <= 599:
        return WebhookResponseDecision(status_code, "retry", retry=True, terminal=False, reason="receiver_error")
    if 300 <= status_code <= 399:
        return WebhookResponseDecision(
            status_code,
            "failed",
            retry=False,
            terminal=True,
            reason="redirect_not_allowed",
        )
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
        _require_sha256_digest("callback payload projection payload_digest", self.payload_digest)
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
        if self.mode == "artifact_reference" and self.payload:
            raise ValueError("artifact_reference callback payload projection must not include inline payload")
        if self.mode == "artifact_reference" and not isinstance(self.artifact, ArtifactRef):
            raise ValueError("artifact_reference callback payload projection requires an ArtifactRef")
        if self.mode == "artifact_reference" and self.payload_size_bytes == 0:
            raise ValueError("artifact_reference callback payload_size_bytes must be positive")


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
    validation = validate_webhook_url(url, allow_private=allow_private)
    return WebhookTargetSafety(
        url=url,
        allowed=validation.allowed,
        reason=validation.reason,
        host=validation.host,
    )


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
            max(1, _non_negative_int("initial_delay_ms", self.initial_delay_ms)),
        )
        object.__setattr__(
            self,
            "max_delay_ms",
            max(1, _non_negative_int("max_delay_ms", self.max_delay_ms)),
        )
        object.__setattr__(self, "jitter_ms", _non_negative_int("jitter_ms", self.jitter_ms))
        if self.initial_delay_ms > self.max_delay_ms:
            object.__setattr__(self, "max_delay_ms", self.initial_delay_ms)

    def delay_ms(self, *, delivery_id: str, attempt: int) -> int:
        _require_stable_string("delivery_id", delivery_id)
        attempt = _positive_int("attempt", attempt)
        exponent = attempt - 1
        if exponent >= self.max_delay_ms.bit_length():
            base = self.max_delay_ms
        else:
            base = min(self.max_delay_ms, self.initial_delay_ms << exponent)
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
            _require_stable_string(field_name, getattr(self, field_name))
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
        if self.status != "acknowledged" and acknowledged_at is not None:
            raise ValueError("acknowledged_at requires acknowledged status")
        if self.status in {"failed", "dead_lettered", "cancelled", "expired"} and self.last_error is None:
            raise ValueError("terminal failure callback delivery requires last_error")
        if self.status in {"pending", "delivering"} and delivered_at is not None:
            raise ValueError(f"{self.status} callback delivery must not already have delivered_at")
        if self.status == "delivered" and delivered_at is None:
            raise ValueError("delivered callback delivery requires delivered_at")
        if self.status == "acknowledged" and delivered_at is None:
            raise ValueError("acknowledged callback delivery requires delivered_at")
        if self.status == "acknowledged" and acknowledged_at is None:
            raise ValueError("acknowledged callback delivery requires acknowledged_at")
        if _is_terminal_delivery(self.status, self.next_retry_at) and self.next_retry_at is not None:
            raise ValueError("terminal callback delivery must not have next_retry_at")

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
        if _is_terminal_delivery(self.status, self.next_retry_at):
            raise ValueError("terminal callback delivery cannot apply webhook response")
        if self.status != "delivering":
            raise ValueError("webhook response requires delivering callback delivery")

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
                if retry_after > received:
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
        _require_stable_string("run_id", self.run_id)
        _require_stable_string("delivery_id", self.delivery_id)
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
    if delivery.status not in TERMINAL_FAILURE_DELIVERY_STATUSES or not _is_terminal_delivery(
        delivery.status,
        delivery.next_retry_at,
    ):
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
            if field_name == "reason":
                _require_non_empty_string(field_name, getattr(self, field_name))
            elif field_name == "redriven_at":
                _require_non_empty_string(field_name, getattr(self, field_name))
            else:
                _require_stable_string(field_name, getattr(self, field_name))
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
        if self.attempt_history != tuple(range(1, len(self.attempt_history) + 1)):
            raise ValueError("dead-letter record attempt_history must be consecutive from attempt 1")
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
        ):
            _require_stable_string(field_name, getattr(self, field_name))
        _require_sha256_digest("envelope_digest", self.envelope_digest)


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
        _require_sha256_digest("incoming_digest", self.incoming_digest)
        if not isinstance(self.duplicate, bool):
            raise ValueError("duplicate must be a boolean")
        if not isinstance(self.conflict, bool):
            raise ValueError("conflict must be a boolean")
        if self.duplicate and self.conflict:
            raise ValueError("callback replay decision cannot be both duplicate and conflict")
        if self.status == "accepted" and (self.duplicate or self.conflict):
            raise ValueError("accepted replay decision must not be duplicate or conflict")
        if self.status == "duplicate" and not self.duplicate:
            raise ValueError("duplicate replay decision must set only duplicate")
        if self.status == "conflict" and not self.conflict:
            raise ValueError("conflict replay decision must set only conflict")


class CallbackReplayGuard:
    def __init__(self, records: Mapping[str, CallbackReplayRecord] | None = None) -> None:
        self._lock = RLock()
        self._records_by_delivery_id: dict[str, CallbackReplayRecord] = {}
        self._records_by_subscription_event: dict[tuple[str, str], CallbackReplayRecord] = {}
        if records is None:
            self._records: dict[str, CallbackReplayRecord] = {}
            return
        if not isinstance(records, Mapping):
            raise ValueError("records must be a mapping")
        self._records = {}
        for key, record in records.items():
            _require_stable_string("record key", key)
            if not isinstance(record, CallbackReplayRecord):
                raise ValueError("records values must be CallbackReplayRecord")
            if key != record.idempotency_key:
                raise ValueError("record key must match record idempotency_key")
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
        with self._lock:
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
        with self._lock:
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
        _require_stable_string("kind", self.kind)
        if self.kind not in VALID_CALLBACK_AUTH_KINDS:
            raise ValueError("kind must be bearer, hmac, mtls, or oidc")
        for field_name in ("token_ref", "secret_ref", "client_identity_ref", "issuer", "audience"):
            value = getattr(self, field_name)
            if value is not None:
                _require_stable_string(field_name, value)
        if self.kind == "bearer" and self.token_ref is None:
            raise ValueError("bearer callback auth requires token_ref")
        if self.kind == "hmac" and self.secret_ref is None:
            raise ValueError("hmac callback auth requires secret_ref")
        if self.kind == "mtls" and self.client_identity_ref is None:
            raise ValueError("mtls callback auth requires client_identity_ref")
        if self.kind == "oidc" and (self.issuer is None or self.audience is None):
            raise ValueError("oidc callback auth requires issuer and audience")
        allowed_fields = {
            "bearer": frozenset({"token_ref"}),
            "hmac": frozenset({"secret_ref"}),
            "mtls": frozenset({"client_identity_ref"}),
            "oidc": frozenset({"issuer", "audience"}),
        }[self.kind]
        for field_name in ("token_ref", "secret_ref", "client_identity_ref", "issuer", "audience"):
            if field_name not in allowed_fields and getattr(self, field_name) is not None:
                raise ValueError(f"{self.kind} callback auth must not define {field_name}")


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
    provider_operation_id: str | None = None

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
            _require_stable_string(field_name, getattr(self, field_name))
        url_validation = validate_webhook_url(self.url, allow_private=True)
        if url_validation.reason in {"unsupported_scheme", "missing_host"}:
            raise ValueError("url must be an absolute http(s) URL")
        if url_validation.reason == "userinfo_not_allowed":
            raise ValueError("url must not contain embedded userinfo")
        if not url_validation.allowed:
            raise ValueError("url host is malformed")
        if not isinstance(self.auth, CallbackEndpointAuth):
            raise ValueError("auth must be a CallbackEndpointAuth")
        if self.expires_at is not None:
            _parse_field_timestamp("expires_at", self.expires_at)
        if self.provider_operation_id is not None:
            _require_stable_string("provider_operation_id", self.provider_operation_id)

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
    operation_id: str | None = None

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
            if field_name in {"occurred_at", "delivered_at"}:
                _require_non_empty_string(field_name, getattr(self, field_name))
            else:
                _require_stable_string(field_name, getattr(self, field_name))
        if self.tenant_id is not None:
            _require_stable_string("tenant_id", self.tenant_id)
        if self.operation_id is not None:
            _require_stable_string("operation_id", self.operation_id)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
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
            "payload": _thaw_json_value(self.payload),
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
            "delivered_at": self.delivered_at,
            "release_id": self.release_id,
        }
        if self.tenant_id is not None:
            payload["tenant_id"] = self.tenant_id
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
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


class CallbackSecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> bytes:
        ...


@dataclass(frozen=True, slots=True)
class WebhookTransportResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_code", _validate_http_status_code(self.status_code))
        object.__setattr__(self, "headers", _string_headers(self.headers))


class CallbackWebhookTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: dict[str, str],
        resolved_addresses: tuple[str, ...],
    ) -> WebhookTransportResponse:
        ...


@dataclass(frozen=True, slots=True)
class RegisteredSecretWebhookDispatcher:
    secret_resolver: CallbackSecretResolver
    transport: CallbackWebhookTransport
    delivered_at_factory: Callable[[], str] = _utc_now_iso
    hostname_resolver: Callable[[str, int], tuple[str, ...]] | None = None

    def deliver(
        self,
        registration: ServerCallbackRegistration,
        event: Mapping[str, object],
    ) -> ServerCallbackDeliveryResult:
        if not isinstance(registration, ServerCallbackRegistration):
            raise ValueError("registration must be a ServerCallbackRegistration")
        if not isinstance(event, Mapping):
            raise ValueError("event must be a mapping")
        delivery = registration.delivery
        if delivery.get("kind") != "webhook":
            raise ValueError("registered secret webhook dispatcher requires webhook delivery")
        metadata = event.get("metadata")
        payload = event.get("payload")
        if not isinstance(metadata, Mapping):
            raise ValueError("callback delivery event metadata must be a mapping")
        if not isinstance(payload, Mapping):
            raise ValueError("callback delivery event payload must be a mapping")
        event_id = metadata.get("eventId")
        run_id = metadata.get("runId")
        sequence = metadata.get("sequence")
        cursor = metadata.get("cursor")
        event_type = event.get("kind")
        occurred_at = metadata.get("occurredAt")
        release_id = metadata.get("releaseId", "local")
        for field_name, value in (
            ("event_id", event_id),
            ("run_id", run_id),
            ("cursor", cursor),
            ("event type", event_type),
            ("occurred_at", occurred_at),
            ("release_id", release_id),
        ):
            if not isinstance(value, str):
                raise ValueError(f"callback delivery {field_name} must be a string")
            _require_stable_string(field_name, value)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("callback delivery sequence must be a non-negative integer")

        encoded_subscription_id = ""
        for byte in registration.subscription_id.encode("utf-8"):
            if chr(byte).isalnum() and byte < 128 or byte in {ord("-"), ord(".")}:
                encoded_subscription_id += chr(byte)
            else:
                encoded_subscription_id += f"%{byte:02X}"
        encoded_event_id = ""
        for byte in event_id.encode("utf-8"):
            if chr(byte).isalnum() and byte < 128 or byte in {ord("-"), ord(".")}:
                encoded_event_id += chr(byte)
            else:
                encoded_event_id += f"%{byte:02X}"
        delivery_id = f"del_{encoded_subscription_id}_{encoded_event_id}"
        idempotency_key = f"{encoded_subscription_id}:{encoded_event_id}"
        delivery_url = str(delivery["url"])
        initial_url_validation = validate_webhook_url(delivery_url)
        try:
            parsed_delivery_url = urlparse(delivery_url)
            delivery_host = initial_url_validation.host
            delivery_port = parsed_delivery_url.port or (443 if parsed_delivery_url.scheme == "https" else 80)
            if not initial_url_validation.allowed or delivery_host is None:
                raise ValueError("unsafe webhook URL")
            if self.hostname_resolver is None:
                resolved_addresses = tuple(
                    dict.fromkeys(
                        str(address[4][0])
                        for address in socket.getaddrinfo(
                            delivery_host,
                            delivery_port,
                            type=socket.SOCK_STREAM,
                        )
                    )
                )
            else:
                resolved_addresses = self.hostname_resolver(delivery_host, delivery_port)
            resolved_url_validation = validate_webhook_url(
                delivery_url,
                resolved_addresses=resolved_addresses,
            )
            if not resolved_url_validation.allowed or not resolved_url_validation.resolved_addresses:
                raise ValueError("unsafe resolved webhook URL")
        except Exception:
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="unsafe_webhook_target",
            )
        signing = delivery.get("signing")
        if not isinstance(signing, Mapping):
            raise ValueError("webhook delivery signing must be a mapping")
        secret_ref = signing.get("secret_ref", signing.get("secretRef"))
        if not isinstance(secret_ref, str):
            raise ValueError("webhook delivery secret_ref must be a string")
        _require_stable_string("secret_ref", secret_ref)
        algorithm = signing.get("algorithm")
        if algorithm != "hmac-sha256":
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="unsupported_signing_algorithm",
            )
        try:
            secret = self.secret_resolver.resolve(secret_ref)
        except Exception:
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="secret_resolution_failed",
            )
        if not isinstance(secret, bytes) or not secret:
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="secret_resolution_failed",
            )
        delivered_at = self.delivered_at_factory()
        envelope = CallbackEnvelope(
            delivery_id=delivery_id,
            subscription_id=registration.subscription_id,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            cursor=cursor,
            type=event_type,
            payload=dict(payload),
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
            delivered_at=delivered_at,
            release_id=release_id,
            tenant_id=registration.owner.tenant_id if registration.owner is not None else None,
            operation_id=(
                metadata.get("operationId")
                if isinstance(metadata.get("operationId"), str)
                else None
            ),
        )
        key_id = signing.get("key_id", signing.get("keyId"))
        if key_id is not None and not isinstance(key_id, str):
            raise ValueError("webhook delivery key_id must be a string")
        try:
            headers = webhook_headers_hmac_sha256(
                envelope,
                secret,
                timestamp=delivered_at,
                key_id=key_id,
            )
        except (TypeError, ValueError):
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="signing_failed",
            )
        headers["Content-Type"] = "application/json"
        try:
            response = self.transport.post(
                delivery_url,
                body=envelope.canonical_body(),
                headers=headers,
                resolved_addresses=resolved_url_validation.resolved_addresses,
            )
        except Exception:
            return ServerCallbackDeliveryResult(
                delivery_id=delivery_id,
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                cursor=cursor,
                attempt=1,
                idempotency_key=idempotency_key,
                status="failed",
                last_error="transport_failed",
            )
        if not isinstance(response, WebhookTransportResponse):
            raise ValueError("webhook transport must return a WebhookTransportResponse")
        decision = classify_webhook_response(
            response.status_code,
            headers=response.headers,
            received_at=delivered_at,
        )
        if decision.status == "delivered":
            result_status = "delivered"
            last_error = None
        elif decision.status == "acknowledged":
            result_status = "acknowledged"
            last_error = None
        elif decision.status == "gone":
            result_status = "cancelled"
            last_error = decision.reason
        elif decision.retry:
            result_status = "pending"
            last_error = decision.reason
        else:
            result_status = "failed"
            last_error = decision.reason
        return ServerCallbackDeliveryResult(
            delivery_id=delivery_id,
            subscription_id=registration.subscription_id,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            cursor=cursor,
            attempt=1,
            idempotency_key=idempotency_key,
            status=result_status,
            status_code=response.status_code,
            delivered_at=delivered_at,
            last_error=last_error,
        )


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
            if field_name == "received_at":
                _require_non_empty_string(field_name, getattr(self, field_name))
            else:
                _require_stable_string(field_name, getattr(self, field_name))
        if self.verified_by.strip().lower() == "unauthenticated":
            raise ValueError("verified_by must identify an authenticated verifier")
        _require_sha256_digest("external callback receipt payload_digest", self.payload_digest)
        if self.tenant_id is not None:
            _require_stable_string("tenant_id", self.tenant_id)
        if self.provider_operation_id is not None:
            _require_stable_string("provider_operation_id", self.provider_operation_id)
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
    if envelope.type != "ExternalCallbackReceived":
        raise ValueError("envelope type must be ExternalCallbackReceived")
    expected_payload_digest = canonical_hash(envelope.payload)
    if payload_projection.payload_digest != expected_payload_digest:
        raise ValueError("payload_projection must match the envelope payload")
    if _parse_field_timestamp("received_at", received_at) < _parse_field_timestamp(
        "envelope delivered_at",
        envelope.delivered_at,
    ):
        raise ValueError("received_at must not be before envelope delivered_at")
    _require_stable_string("operation_id", operation_id)
    if envelope.operation_id is not None and operation_id != envelope.operation_id:
        raise ValueError("operation_id must match the envelope")
    if run_id is not None:
        _require_stable_string("run_id", run_id)
        if run_id != envelope.run_id:
            raise ValueError("run_id must match the envelope")
    if release_id is not None:
        _require_stable_string("release_id", release_id)
        if release_id != envelope.release_id:
            raise ValueError("release_id must match the envelope")
    if tenant_id is not None:
        _require_stable_string("tenant_id", tenant_id)
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
        if self.status == "admitted" and not self.can_resume:
            raise ValueError("admitted callback resume decision must set can_resume")
        if self.status != "admitted" and self.can_resume:
            raise ValueError("non-admitted callback resume decision must not set can_resume")
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
    now_at = _parse_utc_timestamp(now)

    endpoint_binding_key = endpoint.binding_key()
    receipt_binding_key = receipt.binding_key()
    if endpoint.expires_at is not None:
        endpoint_expires_at = _parse_utc_timestamp(endpoint.expires_at)
        if now_at >= endpoint_expires_at:
            return CallbackResumeDecision(
                status="expired",
                can_resume=False,
                reason="callback_endpoint_expired",
                endpoint_binding_key=endpoint_binding_key,
                receipt_binding_key=receipt_binding_key,
            )
        if _parse_utc_timestamp(receipt.received_at) >= endpoint_expires_at:
            return CallbackResumeDecision(
                status="expired",
                can_resume=False,
                reason="callback_received_after_endpoint_expiration",
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
    if (
        endpoint.provider_operation_id is not None
        and endpoint.provider_operation_id != receipt.provider_operation_id
    ):
        return CallbackResumeDecision(
            status="stale",
            can_resume=False,
            reason="callback_provider_operation_mismatch",
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
    "CallbackSecretResolver",
    "CallbackWebhookTransport",
    "ExternalCallbackReceipt",
    "REQUIRED_WEBHOOK_HEADERS",
    "RegisteredSecretWebhookDispatcher",
    "WebhookTargetSafety",
    "WebhookResponseDecision",
    "WebhookTransportResponse",
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
