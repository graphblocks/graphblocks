from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CALLBACKS_SRC = ROOT / "packages" / "graphblocks-callbacks" / "src"
if str(CALLBACKS_SRC) not in sys.path:
    sys.path.insert(0, str(CALLBACKS_SRC))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


from graphblocks_callbacks import (  # noqa: E402
    CallbackEnvelope,
    REQUIRED_WEBHOOK_HEADERS,
    verify_webhook_hmac_sha256,
    webhook_headers_hmac_sha256,
)


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
