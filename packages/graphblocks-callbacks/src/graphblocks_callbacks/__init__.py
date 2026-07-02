from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import math

from graphblocks import canonical_dumps, canonical_hash


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_non_empty_string(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


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


def _json_payload(value: Mapping[str, object]) -> dict[str, object]:
    _validate_json_value(dict(value))
    json.dumps(value, allow_nan=False)
    return deepcopy(dict(value))


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
        _require_non_empty_string("timestamp", timestamp)
        return {
            "GraphBlocks-Delivery-Id": self.delivery_id,
            "GraphBlocks-Event-Id": self.event_id,
            "GraphBlocks-Run-Id": self.run_id,
            "GraphBlocks-Cursor": self.cursor,
            "GraphBlocks-Idempotency-Key": self.idempotency_key,
            "GraphBlocks-Timestamp": timestamp,
        }


def sign_webhook_hmac_sha256(envelope: CallbackEnvelope, secret: bytes, *, timestamp: str | None = None) -> str:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    timestamp = envelope.delivered_at if timestamp is None else timestamp
    _require_non_empty_string("timestamp", timestamp)
    body = timestamp.encode("utf-8") + b"." + envelope.canonical_body()
    return hmac.digest(secret, body, "sha256").hex()


def webhook_headers_hmac_sha256(
    envelope: CallbackEnvelope,
    secret: bytes,
    *,
    timestamp: str | None = None,
) -> dict[str, str]:
    timestamp = envelope.delivered_at if timestamp is None else timestamp
    headers = envelope.unsigned_headers(timestamp=timestamp)
    headers["GraphBlocks-Signature"] = sign_webhook_hmac_sha256(
        envelope,
        secret,
        timestamp=timestamp,
    )
    headers["GraphBlocks-Signature-Algorithm"] = "hmac-sha256"
    return headers


def verify_webhook_hmac_sha256(
    envelope: CallbackEnvelope,
    secret: bytes,
    signature: str,
    *,
    timestamp: str | None = None,
) -> bool:
    _require_non_empty_string("signature", signature)
    expected = sign_webhook_hmac_sha256(envelope, secret, timestamp=timestamp)
    return hmac.compare_digest(expected, signature)


__all__ = [
    "CallbackEnvelope",
    "REQUIRED_WEBHOOK_HEADERS",
    "sign_webhook_hmac_sha256",
    "verify_webhook_hmac_sha256",
    "webhook_headers_hmac_sha256",
]
