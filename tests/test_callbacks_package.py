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


from graphblocks_callbacks import (  # noqa: E402
    CallbackEnvelope,
    CallbackDeliveryProjection,
    CallbackRetryPolicy,
    REQUIRED_WEBHOOK_HEADERS,
    verify_webhook_headers_hmac_sha256,
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
