from __future__ import annotations

import math
import random
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CALLBACKS_SRC = ROOT / "packages" / "graphblocks-callbacks" / "src"
if str(CALLBACKS_SRC) not in sys.path:
    sys.path.insert(0, str(CALLBACKS_SRC))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


from graphblocks import ArtifactRef  # noqa: E402
from graphblocks_callbacks import (  # noqa: E402
    CallbackDeadLetterRecord,
    CallbackEndpointAuth,
    CallbackEndpointRef,
    CallbackEnvelope,
    CallbackDeliveryProjection,
    CallbackPayloadProjection,
    CallbackResumeDecision,
    CallbackReplayGuard,
    CallbackRetryPolicy,
    ExternalCallbackReceipt,
    REQUIRED_WEBHOOK_HEADERS,
    WebhookTargetSafety,
    classify_webhook_response,
    evaluate_callback_resume,
    project_callback_payload,
    record_external_callback_receipt,
    validate_webhook_target_url,
    verify_webhook_headers_hmac_sha256,
    verify_webhook_headers_hmac_sha256_keyring,
    verify_webhook_hmac_sha256,
    webhook_headers_hmac_sha256,
)


def _assert_raises_value_error(match: str, callback: Callable[[], object]) -> None:
    try:
        callback()
    except ValueError as exc:
        assert match in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_callback_envelope_projects_required_webhook_headers() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
        release_id="rel_001",
    )

    payload = envelope.to_payload()
    headers = webhook_headers_hmac_sha256(envelope, b"callback-secret")

    assert payload["payload"] == {"subject": "changeset_abc"}
    assert payload["idempotency_key"] == "sub_001:evt_1042"
    assert set(REQUIRED_WEBHOOK_HEADERS).issubset(headers)
    assert headers["GraphBlocks-Delivery-Id"] == "del_001"
    assert headers["GraphBlocks-Signature-Algorithm"] == "hmac-sha256"
    assert verify_webhook_hmac_sha256(
        envelope,
        b"callback-secret",
        headers["GraphBlocks-Signature"],
    )


def test_callback_envelope_deep_copies_payload() -> None:
    source = {"summary": {"files": ["a.py"]}}
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload=source,
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
    )

    source["summary"]["files"].append("b.py")  # type: ignore[index, union-attr]
    projected = envelope.to_payload()
    projected["payload"]["summary"]["files"].append("c.py")  # type: ignore[index, union-attr]

    assert envelope.to_payload()["payload"] == {"summary": {"files": ["a.py"]}}


def test_callback_envelope_rejects_non_json_payload_values() -> None:
    _assert_raises_value_error(
        "payload must contain only string object keys",
        lambda: CallbackEnvelope(
            delivery_id="del_001",
            subscription_id="sub_001",
            event_id="evt_1042",
            run_id="run_coding_001",
            sequence=1042,
            cursor="evt_1042",
            type="ReviewRequested",
            payload={1: "not-json-object-key"},  # type: ignore[dict-item]
            idempotency_key="sub_001:evt_1042",
            occurred_at="2026-07-02T00:00:00Z",
        ),
    )
    _assert_raises_value_error(
        "payload must not contain non-finite numbers",
        lambda: CallbackEnvelope(
            delivery_id="del_001",
            subscription_id="sub_001",
            event_id="evt_1042",
            run_id="run_coding_001",
            sequence=1042,
            cursor="evt_1042",
            type="ReviewRequested",
            payload={"value": math.nan},
            idempotency_key="sub_001:evt_1042",
            occurred_at="2026-07-02T00:00:00Z",
        ),
    )


def test_callback_envelope_deterministic_fuzz_signatures_survive_reordering_and_mutation() -> None:
    rng = random.Random(6016)

    for case in range(100):
        keys = [f"k_{index:02d}" for index in range(rng.randint(2, 8))]
        values = {
            key: {
                "number": rng.randint(0, 1_000_000),
                "flag": bool(rng.getrandbits(1)),
                "items": [rng.choice(["alpha", "beta", "gamma"]), rng.randint(0, 99)],
            }
            for key in keys
        }
        shuffled_keys = keys[:]
        rng.shuffle(shuffled_keys)
        ordered_payload = {key: values[key] for key in keys}
        reordered_payload = {key: values[key] for key in shuffled_keys}

        envelope = CallbackEnvelope(
            delivery_id=f"del_{case:03d}",
            subscription_id="sub_fuzz",
            event_id=f"evt_{case:03d}",
            run_id="run_fuzz",
            sequence=case,
            cursor=f"evt_{case:03d}",
            type="FuzzEvent",
            payload=ordered_payload,
            idempotency_key=f"sub_fuzz:evt_{case:03d}",
            occurred_at="2026-07-02T00:00:00Z",
            delivered_at="2026-07-02T00:00:01Z",
        )
        reordered_envelope = CallbackEnvelope(
            delivery_id=f"del_{case:03d}",
            subscription_id="sub_fuzz",
            event_id=f"evt_{case:03d}",
            run_id="run_fuzz",
            sequence=case,
            cursor=f"evt_{case:03d}",
            type="FuzzEvent",
            payload=reordered_payload,
            idempotency_key=f"sub_fuzz:evt_{case:03d}",
            occurred_at="2026-07-02T00:00:00Z",
            delivered_at="2026-07-02T00:00:01Z",
        )

        before = webhook_headers_hmac_sha256(envelope, b"callback-secret")
        ordered_payload[keys[0]]["items"].append("mutated")  # type: ignore[index, union-attr]
        after = webhook_headers_hmac_sha256(envelope, b"callback-secret")
        reordered = webhook_headers_hmac_sha256(reordered_envelope, b"callback-secret")

        assert before == after
        assert before == reordered


def test_callback_webhook_header_verification_accepts_valid_signed_request() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    headers = webhook_headers_hmac_sha256(envelope, b"callback-secret")

    assert verify_webhook_headers_hmac_sha256(
        envelope,
        headers,
        b"callback-secret",
        now="2026-07-02T00:00:31Z",
        replay_window_seconds=60,
    )


def test_callback_webhook_header_verification_rejects_tampering_and_stale_timestamps() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    headers = webhook_headers_hmac_sha256(envelope, b"callback-secret")

    missing = dict(headers)
    del missing["GraphBlocks-Signature"]
    tampered = {**headers, "GraphBlocks-Event-Id": "evt_other"}

    assert not verify_webhook_headers_hmac_sha256(
        envelope,
        missing,
        b"callback-secret",
        now="2026-07-02T00:00:31Z",
        replay_window_seconds=60,
    )
    assert not verify_webhook_headers_hmac_sha256(
        envelope,
        tampered,
        b"callback-secret",
        now="2026-07-02T00:00:31Z",
        replay_window_seconds=60,
    )
    assert not verify_webhook_headers_hmac_sha256(
        envelope,
        headers,
        b"callback-secret",
        now="2026-07-02T00:02:02Z",
        replay_window_seconds=60,
    )


def test_callback_webhook_hmac_headers_include_optional_key_id() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )

    headers = webhook_headers_hmac_sha256(envelope, b"callback-secret", key_id="current")

    assert headers["GraphBlocks-Key-Id"] == "current"
    assert verify_webhook_headers_hmac_sha256_keyring(
        envelope,
        headers,
        {"current": b"callback-secret", "previous": b"old-secret"},
        now="2026-07-02T00:00:31Z",
        replay_window_seconds=60,
    ) == "current"


def test_callback_webhook_hmac_keyring_accepts_previous_key_during_rotation() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    headers = webhook_headers_hmac_sha256(envelope, b"old-secret", key_id="previous")

    assert verify_webhook_headers_hmac_sha256_keyring(
        envelope,
        headers,
        {"current": b"callback-secret", "previous": b"old-secret"},
        now="2026-07-02T00:00:31Z",
        replay_window_seconds=60,
    ) == "previous"
    assert (
        verify_webhook_headers_hmac_sha256_keyring(
            envelope,
            {**headers, "GraphBlocks-Key-Id": "missing"},
            {"current": b"callback-secret", "previous": b"old-secret"},
            now="2026-07-02T00:00:31Z",
            replay_window_seconds=60,
        )
        is None
    )


def test_callback_retry_policy_schedules_bounded_deterministic_backoff() -> None:
    policy = CallbackRetryPolicy(
        max_attempts=4,
        initial_delay_ms=100,
        max_delay_ms=1_000,
        jitter_ms=25,
    )
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=1,
        idempotency_key="sub_001:evt_1042",
        status="failed",
    )

    first_retry = delivery.schedule_retry(
        policy,
        failed_at="2026-07-02T00:00:00Z",
        error="receiver 503",
    )
    second_retry = first_retry.mark_failed("receiver 503").schedule_retry(
        policy,
        failed_at="2026-07-02T00:00:00Z",
        error="receiver 503 again",
    )

    assert first_retry.status == "pending"
    assert first_retry.attempt == 2
    assert first_retry.next_retry_at == "2026-07-02T00:00:00.221Z"
    assert first_retry.last_error == "receiver 503"
    assert second_retry.attempt == 3
    assert second_retry.next_retry_at == "2026-07-02T00:00:00.408Z"
    assert first_retry.schedule_retry(
        policy,
        failed_at="2026-07-02T00:00:00Z",
        error="receiver 503",
    ) == first_retry


def test_callback_dead_letter_and_redrive_preserve_delivery_identity_and_attempt_history() -> None:
    policy = CallbackRetryPolicy(max_attempts=2, initial_delay_ms=100, max_delay_ms=1_000, jitter_ms=0)
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=2,
        idempotency_key="sub_001:evt_1042",
        status="failed",
        last_error="receiver 503",
    )

    dead_letter = delivery.to_dead_letter(
        policy,
        dead_lettered_at="2026-07-02T00:00:30Z",
        reason="retry exhausted",
    )
    redrive = dead_letter.redrive(
        operator_principal="operator-1",
        reason="receiver fixed",
        redriven_at="2026-07-02T00:01:00Z",
    )

    assert dead_letter.delivery.delivery_id == "del_001"
    assert dead_letter.delivery.idempotency_key == "sub_001:evt_1042"
    assert dead_letter.attempt_history == (1, 2)
    assert redrive.delivery_id == "del_001"
    assert redrive.event_id == "evt_1042"
    assert redrive.subscription_id == "sub_001"
    assert redrive.attempt_history == (1, 2)
    assert redrive.operator_principal == "operator-1"
    assert redrive.reason == "receiver fixed"


def test_callback_dead_letter_redrive_creates_pending_delivery_without_new_event_identity() -> None:
    policy = CallbackRetryPolicy(max_attempts=2, initial_delay_ms=100, max_delay_ms=1_000, jitter_ms=0)
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=2,
        idempotency_key="sub_001:evt_1042",
        status="failed",
        delivered_at="2026-07-02T00:00:10Z",
        last_error="receiver 503",
    )
    dead_letter = delivery.to_dead_letter(
        policy,
        dead_lettered_at="2026-07-02T00:00:30Z",
        reason="retry exhausted",
    )

    redriven = dead_letter.redrive_delivery(
        redriven_at="2026-07-02T00:01:00Z",
        reason="operator redrive",
    )

    assert redriven.delivery_id == "del_001"
    assert redriven.subscription_id == "sub_001"
    assert redriven.event_id == "evt_1042"
    assert redriven.run_id == "run_coding_001"
    assert redriven.sequence == 1042
    assert redriven.cursor == "evt_1042"
    assert redriven.idempotency_key == "sub_001:evt_1042"
    assert redriven.status == "pending"
    assert redriven.attempt == 3
    assert redriven.next_retry_at == "2026-07-02T00:01:00Z"
    assert redriven.delivered_at is None
    assert redriven.acknowledged_at is None
    assert redriven.last_error == "operator redrive"


def test_callback_dead_letter_record_rejects_inconsistent_delivery_state() -> None:
    _assert_raises_value_error(
        "dead-letter record delivery must have dead_lettered status",
        lambda: CallbackDeadLetterRecord(
            delivery=CallbackDeliveryProjection(
                delivery_id="del_001",
                subscription_id="sub_001",
                event_id="evt_1042",
                run_id="run_coding_001",
                sequence=1042,
                cursor="evt_1042",
                attempt=2,
                idempotency_key="sub_001:evt_1042",
                status="failed",
                last_error="receiver 503",
            ),
            attempt_history=(1, 2),
            dead_lettered_at="2026-07-02T00:00:30Z",
            reason="retry exhausted",
        ),
    )
    _assert_raises_value_error(
        "dead-letter record attempt_history must include delivery attempt",
        lambda: CallbackDeadLetterRecord(
            delivery=CallbackDeliveryProjection(
                delivery_id="del_002",
                subscription_id="sub_001",
                event_id="evt_1042",
                run_id="run_coding_001",
                sequence=1042,
                cursor="evt_1042",
                attempt=3,
                idempotency_key="sub_001:evt_1042:missing-attempt",
                status="dead_lettered",
                last_error="receiver 503",
            ),
            attempt_history=(1, 2),
            dead_lettered_at="2026-07-02T00:00:30Z",
            reason="retry exhausted",
        ),
    )


def test_callback_delivery_projection_validates_timestamp_fields() -> None:
    _assert_raises_value_error(
        "next_retry_at must be an ISO-8601 datetime",
        lambda: CallbackDeliveryProjection(
            delivery_id="del_001",
            subscription_id="sub_001",
            event_id="evt_1042",
            run_id="run_coding_001",
            sequence=1042,
            cursor="evt_1042",
            attempt=1,
            idempotency_key="sub_001:evt_1042",
            status="pending",
            next_retry_at="soon",
        ),
    )
    _assert_raises_value_error(
        "acknowledged_at must not be before delivered_at",
        lambda: CallbackDeliveryProjection(
            delivery_id="del_002",
            subscription_id="sub_001",
            event_id="evt_1042",
            run_id="run_coding_001",
            sequence=1042,
            cursor="evt_1042",
            attempt=1,
            idempotency_key="sub_001:evt_1042:ack",
            status="acknowledged",
            delivered_at="2026-07-02T00:00:10Z",
            acknowledged_at="2026-07-02T00:00:09Z",
        ),
    )


def test_webhook_target_safety_allows_public_https_targets() -> None:
    safety = validate_webhook_target_url("https://callbacks.example.com/graphblocks/events")

    assert safety == WebhookTargetSafety(
        url="https://callbacks.example.com/graphblocks/events",
        allowed=True,
        reason="allowed",
        host="callbacks.example.com",
    )


def test_webhook_target_safety_rejects_forbidden_targets_by_default() -> None:
    cases = {
        "http://localhost/callback": "forbidden_host",
        "https://metadata.google.internal/computeMetadata/v1": "forbidden_host",
        "https://127.0.0.1/callback": "forbidden_ip",
        "https://10.0.0.7/callback": "forbidden_ip",
        "https://169.254.169.254/latest/meta-data": "forbidden_ip",
        "https://user:pass@example.com/callback": "userinfo_not_allowed",
        "file:///tmp/callback.sock": "unsupported_scheme",
        "unix:///var/run/callback.sock": "unsupported_scheme",
    }

    for url, reason in cases.items():
        safety = validate_webhook_target_url(url)
        assert safety.allowed is False
        assert safety.reason == reason


def test_webhook_target_safety_can_allow_private_hosts_explicitly() -> None:
    safety = validate_webhook_target_url("https://10.0.0.7/callback", allow_private=True)

    assert safety.allowed is True
    assert safety.reason == "allowed"
    assert safety.host == "10.0.0.7"


def test_callback_payload_projection_keeps_small_payload_inline() -> None:
    projection = project_callback_payload(
        {"status": "completed", "checks": ["lint", "unit"]},
        max_inline_bytes=256,
    )

    assert projection == CallbackPayloadProjection(
        mode="inline",
        payload={"status": "completed", "checks": ["lint", "unit"]},
        payload_digest=projection.payload_digest,
        payload_size_bytes=47,
    )
    assert projection.artifact is None
    assert projection.payload_digest.startswith("sha256:")


def test_callback_payload_projection_converts_large_payload_to_artifact_ref() -> None:
    artifact = ArtifactRef(
        "artifact-callback-log",
        "blob://callbacks/run-1/log.txt",
        media_type="text/plain",
        size_bytes=2048,
        checksum="sha256:callback-log",
    )
    projection = project_callback_payload(
        {"log": "x" * 200},
        max_inline_bytes=64,
        artifact=artifact,
    )

    assert projection.mode == "artifact_reference"
    assert projection.payload == {}
    assert projection.artifact == artifact
    assert projection.payload_size_bytes > 64
    assert projection.payload_digest.startswith("sha256:")


def test_callback_payload_projection_rejects_oversized_payload_without_artifact_ref() -> None:
    _assert_raises_value_error(
        "oversized callback payload requires an ArtifactRef",
        lambda: project_callback_payload({"log": "x" * 200}, max_inline_bytes=64),
    )


def test_webhook_response_classification_maps_receiver_statuses() -> None:
    assert classify_webhook_response(204).status == "delivered"
    assert classify_webhook_response(409).status == "acknowledged"
    assert classify_webhook_response(410).status == "gone"
    assert classify_webhook_response(400).status == "failed"
    assert classify_webhook_response(503).status == "retry"

    duplicate = classify_webhook_response(409)
    assert duplicate.retry is False
    assert duplicate.terminal is True
    assert duplicate.reason == "duplicate_already_processed"


def test_webhook_response_classification_parses_retry_after() -> None:
    decision = classify_webhook_response(
        429,
        headers={"Retry-After": "15"},
        received_at="2026-07-02T00:00:00Z",
    )

    assert decision.status == "retry"
    assert decision.retry is True
    assert decision.retry_after == "2026-07-02T00:00:15.000Z"
    assert decision.reason == "rate_limited"


def test_webhook_response_classification_rejects_invalid_status_codes_and_headers() -> None:
    _assert_raises_value_error(
        "status_code must be a valid HTTP status",
        lambda: classify_webhook_response(99),
    )
    _assert_raises_value_error(
        "headers values must be strings",
        lambda: classify_webhook_response(429, headers={"Retry-After": object()}),  # type: ignore[dict-item]
    )


def test_callback_delivery_projection_applies_terminal_webhook_responses() -> None:
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=1,
        idempotency_key="sub_001:evt_1042",
        status="delivering",
    )

    delivered = delivery.apply_webhook_response(
        classify_webhook_response(204),
        received_at="2026-07-02T00:00:00Z",
        policy=CallbackRetryPolicy(max_attempts=4),
    )
    duplicate = delivery.apply_webhook_response(
        classify_webhook_response(409),
        received_at="2026-07-02T00:00:01Z",
        policy=CallbackRetryPolicy(max_attempts=4),
    )

    assert delivered.status == "delivered"
    assert delivered.delivered_at == "2026-07-02T00:00:00Z"
    assert delivered.acknowledged_at is None
    assert duplicate.status == "acknowledged"
    assert duplicate.delivered_at == "2026-07-02T00:00:01Z"
    assert duplicate.acknowledged_at == "2026-07-02T00:00:01Z"
    assert duplicate.last_error is None


def test_callback_delivery_projection_applies_retryable_webhook_responses() -> None:
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=1,
        idempotency_key="sub_001:evt_1042",
        status="delivering",
    )
    policy = CallbackRetryPolicy(max_attempts=4, initial_delay_ms=100, max_delay_ms=1_000, jitter_ms=25)

    rate_limited = delivery.apply_webhook_response(
        classify_webhook_response(
            429,
            headers={"Retry-After": "15"},
            received_at="2026-07-02T00:00:00Z",
        ),
        received_at="2026-07-02T00:00:00Z",
        policy=policy,
    )
    receiver_error = delivery.apply_webhook_response(
        classify_webhook_response(503),
        received_at="2026-07-02T00:00:00Z",
        policy=policy,
    )

    assert rate_limited.status == "pending"
    assert rate_limited.attempt == 2
    assert rate_limited.next_retry_at == "2026-07-02T00:00:15.000Z"
    assert rate_limited.last_error == "rate_limited"
    assert receiver_error.status == "pending"
    assert receiver_error.attempt == 2
    assert receiver_error.next_retry_at == "2026-07-02T00:00:00.221Z"
    assert receiver_error.last_error == "receiver_error"


def test_callback_delivery_projection_applies_non_retryable_webhook_response() -> None:
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=1,
        idempotency_key="sub_001:evt_1042",
        status="delivering",
    )

    failed = delivery.apply_webhook_response(
        classify_webhook_response(400),
        received_at="2026-07-02T00:00:00Z",
        policy=CallbackRetryPolicy(max_attempts=4),
    )

    assert failed.status == "failed"
    assert failed.delivered_at == "2026-07-02T00:00:00Z"
    assert failed.last_error == "non_retryable"


def test_callback_delivery_projection_stops_retry_after_max_attempts() -> None:
    delivery = CallbackDeliveryProjection(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        attempt=2,
        idempotency_key="sub_001:evt_1042",
        status="delivering",
    )

    exhausted = delivery.apply_webhook_response(
        classify_webhook_response(503),
        received_at="2026-07-02T00:00:00Z",
        policy=CallbackRetryPolicy(max_attempts=2),
    )

    assert exhausted.status == "failed"
    assert exhausted.attempt == 2
    assert exhausted.next_retry_at is None
    assert exhausted.delivered_at == "2026-07-02T00:00:00Z"
    assert exhausted.last_error == "receiver_error"


def test_callback_delivery_projection_rejects_webhook_response_after_terminal_state() -> None:
    terminal_statuses = ("delivered", "acknowledged", "dead_lettered", "cancelled", "expired")

    for status in terminal_statuses:
        delivery = CallbackDeliveryProjection(
            delivery_id=f"del_{status}",
            subscription_id="sub_001",
            event_id="evt_1042",
            run_id="run_coding_001",
            sequence=1042,
            cursor="evt_1042",
            attempt=1,
            idempotency_key=f"sub_001:evt_1042:{status}",
            status=status,
        )

        _assert_raises_value_error(
            "terminal callback delivery cannot apply webhook response",
            lambda delivery=delivery: delivery.apply_webhook_response(
                classify_webhook_response(204),
                received_at="2026-07-02T00:00:00Z",
                policy=CallbackRetryPolicy(max_attempts=4),
            ),
        )


def test_callback_replay_guard_accepts_first_delivery_and_marks_exact_replay_duplicate() -> None:
    envelope = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    guard = CallbackReplayGuard()

    first = guard.record(envelope)
    duplicate = guard.record(envelope)

    assert first.status == "accepted"
    assert first.duplicate is False
    assert first.conflict is False
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate is True
    assert duplicate.replay_record == first.replay_record


def test_callback_replay_guard_rejects_mutated_idempotency_replay() -> None:
    first = CallbackEnvelope(
        delivery_id="del_001",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "changeset_abc"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    mutated = CallbackEnvelope(
        delivery_id="del_002",
        subscription_id="sub_001",
        event_id="evt_1042",
        run_id="run_coding_001",
        sequence=1042,
        cursor="evt_1042",
        type="ReviewRequested",
        payload={"subject": "mutated"},
        idempotency_key="sub_001:evt_1042",
        occurred_at="2026-07-02T00:00:02Z",
        delivered_at="2026-07-02T00:00:03Z",
    )
    guard = CallbackReplayGuard()

    accepted = guard.record(first)
    conflict = guard.record(mutated)

    assert accepted.status == "accepted"
    assert conflict.status == "conflict"
    assert conflict.conflict is True
    assert conflict.replay_record == accepted.replay_record
    assert conflict.incoming_digest != accepted.replay_record.envelope_digest


def test_external_callback_receipt_projects_verified_callback_metadata() -> None:
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"status": "completed", "checks": [{"name": "unit", "passed": True}]},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
        release_id="rel_001",
        tenant_id="tenant_001",
    )
    projection = project_callback_payload(envelope.payload, max_inline_bytes=256)

    receipt = record_external_callback_receipt(
        envelope,
        projection,
        operation_id="op_ci_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        received_at="2026-07-02T00:00:02Z",
        provider_operation_id="gh_123",
    )

    assert receipt == ExternalCallbackReceipt(
        callback_id="cb_001",
        operation_id="op_ci_001",
        run_id="run_coding_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        provider_operation_id="gh_123",
        idempotency_key="op_ci_001:attempt_001:provider_001",
        payload_projection=projection,
        payload_digest=projection.payload_digest,
        received_at="2026-07-02T00:00:02Z",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        release_id="rel_001",
        tenant_id="tenant_001",
    )


def test_external_callback_receipt_accepts_artifact_backed_large_payload() -> None:
    artifact = ArtifactRef(
        "artifact-callback-log",
        "blob://callbacks/run-1/log.txt",
        media_type="text/plain",
        size_bytes=4096,
        checksum="sha256:callback-log",
    )
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"log": "x" * 200},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    projection = project_callback_payload(envelope.payload, max_inline_bytes=64, artifact=artifact)

    receipt = record_external_callback_receipt(
        envelope,
        projection,
        operation_id="op_ci_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        received_at="2026-07-02T00:00:02Z",
    )

    assert receipt.payload_projection.mode == "artifact_reference"
    assert receipt.payload_projection.artifact == artifact
    assert receipt.payload_digest == projection.payload_digest


def test_external_callback_receipt_rejects_idempotency_key_mismatch() -> None:
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"status": "completed"},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
    )
    projection = project_callback_payload(envelope.payload, max_inline_bytes=256)

    _assert_raises_value_error(
        "idempotency_key must match the envelope",
        lambda: record_external_callback_receipt(
            envelope,
            projection,
            operation_id="op_ci_001",
            node_id="waitCI",
            attempt_id="attempt_001",
            idempotency_key="different",
            verified_by="hmac-sha256:key-current",
            policy_snapshot_id="policy_001",
            received_at="2026-07-02T00:00:02Z",
        ),
    )


def test_callback_endpoint_ref_binds_auth_schema_and_resume_fence_identity() -> None:
    auth = CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci")
    endpoint = CallbackEndpointRef(
        endpoint_id="cbep_ci_001",
        url="https://graphblocks.example.com/v1/callbacks/op_ci_001",
        accepted_schema="schemas/CICallback@1",
        auth=auth,
        operation_id="op_ci_001",
        run_id="run_coding_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        release_id="rel_001",
        tenant_id="tenant_001",
        expires_at="2026-07-02T00:30:00Z",
    )

    assert endpoint.auth == auth
    assert endpoint.operation_id == "op_ci_001"
    assert endpoint.attempt_id == "attempt_001"
    assert endpoint.binding_key() == "tenant_001:rel_001:run_coding_001:waitCI:attempt_001:op_ci_001"


def test_callback_endpoint_auth_requires_kind_specific_credentials() -> None:
    _assert_raises_value_error(
        "hmac callback auth requires secret_ref",
        lambda: CallbackEndpointAuth(kind="hmac"),
    )
    _assert_raises_value_error(
        "bearer callback auth requires token_ref",
        lambda: CallbackEndpointAuth(kind="bearer"),
    )
    _assert_raises_value_error(
        "mtls callback auth requires client_identity_ref",
        lambda: CallbackEndpointAuth(kind="mtls"),
    )
    _assert_raises_value_error(
        "oidc callback auth requires issuer and audience",
        lambda: CallbackEndpointAuth(kind="oidc", issuer="https://issuer.example.com"),
    )


def test_callback_endpoint_ref_requires_complete_resume_identity() -> None:
    _assert_raises_value_error(
        "attempt_id must be a non-empty string",
        lambda: CallbackEndpointRef(
            endpoint_id="cbep_ci_001",
            url="https://graphblocks.example.com/v1/callbacks/op_ci_001",
            accepted_schema="schemas/CICallback@1",
            auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
            operation_id="op_ci_001",
            run_id="run_coding_001",
            node_id="waitCI",
            attempt_id="",
            release_id="rel_001",
            tenant_id="tenant_001",
        ),
    )


def test_callback_resume_admission_accepts_current_endpoint_receipt() -> None:
    endpoint = CallbackEndpointRef(
        endpoint_id="cbep_ci_001",
        url="https://graphblocks.example.com/v1/callbacks/op_ci_001",
        accepted_schema="schemas/CICallback@1",
        auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
        operation_id="op_ci_001",
        run_id="run_coding_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        release_id="rel_001",
        tenant_id="tenant_001",
        expires_at="2026-07-02T00:30:00Z",
    )
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"status": "completed"},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
        release_id="rel_001",
        tenant_id="tenant_001",
    )
    receipt = record_external_callback_receipt(
        envelope,
        project_callback_payload(envelope.payload, max_inline_bytes=256),
        operation_id="op_ci_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        received_at="2026-07-02T00:00:02Z",
    )

    assert evaluate_callback_resume(endpoint, receipt, now="2026-07-02T00:00:03Z") == CallbackResumeDecision(
        status="admitted",
        can_resume=True,
        reason="current_callback",
        endpoint_binding_key="tenant_001:rel_001:run_coding_001:waitCI:attempt_001:op_ci_001",
        receipt_binding_key="tenant_001:rel_001:run_coding_001:waitCI:attempt_001:op_ci_001",
    )


def test_callback_resume_admission_rejects_expired_endpoint() -> None:
    endpoint = CallbackEndpointRef(
        endpoint_id="cbep_ci_001",
        url="https://graphblocks.example.com/v1/callbacks/op_ci_001",
        accepted_schema="schemas/CICallback@1",
        auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
        operation_id="op_ci_001",
        run_id="run_coding_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        release_id="rel_001",
        tenant_id="tenant_001",
        expires_at="2026-07-02T00:30:00Z",
    )
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"status": "completed"},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
        release_id="rel_001",
        tenant_id="tenant_001",
    )
    receipt = record_external_callback_receipt(
        envelope,
        project_callback_payload(envelope.payload, max_inline_bytes=256),
        operation_id="op_ci_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        received_at="2026-07-02T00:00:02Z",
    )

    decision = evaluate_callback_resume(endpoint, receipt, now="2026-07-02T00:30:01Z")

    assert decision.status == "expired"
    assert decision.can_resume is False
    assert decision.reason == "callback_endpoint_expired"


def test_callback_resume_admission_rejects_stale_attempt_receipt() -> None:
    endpoint = CallbackEndpointRef(
        endpoint_id="cbep_ci_001",
        url="https://graphblocks.example.com/v1/callbacks/op_ci_001",
        accepted_schema="schemas/CICallback@1",
        auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
        operation_id="op_ci_001",
        run_id="run_coding_001",
        node_id="waitCI",
        attempt_id="attempt_002",
        release_id="rel_001",
        tenant_id="tenant_001",
        expires_at="2026-07-02T00:30:00Z",
    )
    envelope = CallbackEnvelope(
        delivery_id="cb_001",
        subscription_id="sub_001",
        event_id="evt_callback_001",
        run_id="run_coding_001",
        sequence=77,
        cursor="evt_callback_001",
        type="ExternalCallbackReceived",
        payload={"status": "completed"},
        idempotency_key="op_ci_001:attempt_001:provider_001",
        occurred_at="2026-07-02T00:00:00Z",
        delivered_at="2026-07-02T00:00:01Z",
        release_id="rel_001",
        tenant_id="tenant_001",
    )
    receipt = record_external_callback_receipt(
        envelope,
        project_callback_payload(envelope.payload, max_inline_bytes=256),
        operation_id="op_ci_001",
        node_id="waitCI",
        attempt_id="attempt_001",
        verified_by="hmac-sha256:key-current",
        policy_snapshot_id="policy_001",
        received_at="2026-07-02T00:00:02Z",
    )

    decision = evaluate_callback_resume(endpoint, receipt, now="2026-07-02T00:00:03Z")

    assert decision.status == "stale"
    assert decision.can_resume is False
    assert decision.reason == "callback_binding_mismatch"
    assert "attempt_002" in decision.endpoint_binding_key
    assert "attempt_001" in decision.receipt_binding_key


def test_callback_resume_admission_deterministic_fuzz_rejects_identity_mutations() -> None:
    rng = random.Random(6015)
    fields = ("tenant_id", "release_id", "run_id", "node_id", "attempt_id", "operation_id")

    for case in range(60):
        base = {
            "tenant_id": f"tenant_{case:03d}",
            "release_id": f"rel_{case:03d}",
            "run_id": f"run_{case:03d}",
            "node_id": f"node_{case:03d}",
            "attempt_id": f"attempt_{case:03d}",
            "operation_id": f"op_{case:03d}",
        }
        endpoint = CallbackEndpointRef(
            endpoint_id=f"cbep_{case:03d}",
            url=f"https://graphblocks.example.com/v1/callbacks/op_{case:03d}",
            accepted_schema="schemas/CICallback@1",
            auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
            expires_at="2026-07-02T00:30:00Z",
            **base,
        )
        envelope = CallbackEnvelope(
            delivery_id=f"cb_{case:03d}",
            subscription_id="sub_fuzz",
            event_id=f"evt_callback_{case:03d}",
            run_id=base["run_id"],
            sequence=case,
            cursor=f"evt_callback_{case:03d}",
            type="ExternalCallbackReceived",
            payload={"status": "completed", "case": case},
            idempotency_key=f"{base['operation_id']}:{base['attempt_id']}:provider",
            occurred_at="2026-07-02T00:00:00Z",
            delivered_at="2026-07-02T00:00:01Z",
            release_id=base["release_id"],
            tenant_id=base["tenant_id"],
        )
        receipt = record_external_callback_receipt(
            envelope,
            project_callback_payload(envelope.payload, max_inline_bytes=256),
            operation_id=base["operation_id"],
            node_id=base["node_id"],
            attempt_id=base["attempt_id"],
            verified_by="hmac-sha256:key-current",
            policy_snapshot_id="policy_001",
            received_at="2026-07-02T00:00:02Z",
        )

        assert evaluate_callback_resume(endpoint, receipt, now="2026-07-02T00:00:03Z").status == "admitted"

        mutated_field = rng.choice(fields)
        mutated = dict(base)
        mutated[mutated_field] = f"{mutated[mutated_field]}_stale"
        stale_endpoint = CallbackEndpointRef(
            endpoint_id=f"cbep_stale_{case:03d}",
            url=f"https://graphblocks.example.com/v1/callbacks/op_{case:03d}",
            accepted_schema="schemas/CICallback@1",
            auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
            expires_at="2026-07-02T00:30:00Z",
            **mutated,
        )

        stale = evaluate_callback_resume(stale_endpoint, receipt, now="2026-07-02T00:00:03Z")

        assert stale.status == "stale"
        assert stale.can_resume is False
        assert stale.endpoint_binding_key != stale.receipt_binding_key
