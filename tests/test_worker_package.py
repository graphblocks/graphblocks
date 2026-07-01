from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

from graphblocks.worker import VALID_WORKER_PROTOCOL_MESSAGE_KINDS, VALID_WORKER_STATES


ROOT = Path(__file__).parents[1]


def test_worker_package_reexports_worker_protocol_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-worker" / "src"))
    graphblocks_worker = importlib.import_module("graphblocks_worker")

    advertisement = graphblocks_worker.WorkerAdvertisement.new(
        "worker-local-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [graphblocks_worker.BlockCapability("prompt.render@1")],
    )

    assert graphblocks_worker.admit_worker(advertisement) is None
    assert graphblocks_worker.select_worker_for_block([advertisement], "prompt.render@1") == advertisement
    assert (
        graphblocks_worker.evaluate_worker_admission(
            graphblocks_worker.WorkerAdmissionPolicy.current().require_block("prompt.render@1"),
            advertisement,
        ).admitted
        is True
    )
    request = graphblocks_worker.WorkerInvokeRequest(
        invocation_id="invoke-1",
        run_id="run-1",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=3,
        block="prompt.render@1",
        context=graphblocks_worker.WorkerInvocationContext("release-1", "rev-old"),
        inputs={},
        config={},
    )
    drain_plan = graphblocks_worker.WorkerDrainPlan.for_worker(
        advertisement,
        graphblocks_worker.WorkerDrainPolicy(),
        (graphblocks_worker.WorkerDrainTask("online_request", request, started_at_unix_ms=0),),
        drain_started_at_unix_ms=1,
        now_unix_ms=1,
    )

    assert drain_plan.worker_state == "draining"
    assert drain_plan.decisions[0].disposition == "finish_in_place"
    message = graphblocks_worker.WorkerProtocolMessage.invoke_request("msg-1", 1, request)

    assert graphblocks_worker.WorkerProtocolMessage.from_wire(message.to_wire()) == message
    edge_payload = graphblocks_worker.RemoteEdgePayload.artifact_ref(
        "graphblocks.ai/PdfDocument@1",
        artifact_id="artifact-1",
        uri="s3://graphblocks/documents/source.pdf",
    )

    assert (
        graphblocks_worker.validate_remote_payload(
            edge_payload.to_wire(),
            graphblocks_worker.RemotePayloadLimits(max_inline_bytes=8),
        )
        is None
    )
    assert "RemoteEdgePayload" in graphblocks_worker.__all__
    assert "RemotePayloadInvalidLimitError" in graphblocks_worker.__all__
    assert "WorkerDrainPlan" in graphblocks_worker.__all__
    assert "WorkerProtocolMessage" in graphblocks_worker.__all__
    assert graphblocks_worker.VALID_WORKER_PROTOCOL_MESSAGE_KINDS is VALID_WORKER_PROTOCOL_MESSAGE_KINDS
    assert graphblocks_worker.VALID_WORKER_STATES is VALID_WORKER_STATES
    assert "VALID_WORKER_PROTOCOL_MESSAGE_KINDS" in graphblocks_worker.__all__
    assert "VALID_WORKER_STATES" in graphblocks_worker.__all__


def test_worker_package_native_message_helper_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-worker" / "src"))
    graphblocks_worker = importlib.import_module("graphblocks_worker")
    calls: list[dict[str, object]] = []

    def validate_worker_protocol_message(message: dict[str, object]) -> dict[str, object]:
        calls.append(message)
        return {"ok": True, "contentDigest": "sha256:message", "message": message}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(validate_worker_protocol_message=validate_worker_protocol_message),
    )
    request = graphblocks_worker.WorkerInvokeRequest(
        invocation_id="invoke-1",
        run_id="run-1",
        node_id="render",
        node_attempt_id="render-attempt-1",
        lease_epoch=3,
        block="prompt.render@1",
        context=graphblocks_worker.WorkerInvocationContext("release-1", "rev-old"),
        inputs={"message": {"text": "hi"}},
        config={},
    )
    message = graphblocks_worker.WorkerProtocolMessage.invoke_request("msg-1", 1, request)

    result = graphblocks_worker.validate_worker_protocol_message_native(message)
    mapping_result = graphblocks_worker.validate_worker_protocol_message_native(message.to_wire())

    assert result["contentDigest"] == "sha256:message"
    assert mapping_result["contentDigest"] == "sha256:message"
    assert calls == [message.to_wire(), message.to_wire()]
    assert "validate_worker_protocol_message_native" in graphblocks_worker.__all__


def test_worker_package_native_advertisement_helper_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-worker" / "src"))
    graphblocks_worker = importlib.import_module("graphblocks_worker")
    calls: list[tuple[dict[str, object], str | None]] = []

    def validate_worker_advertisement(
        advertisement: dict[str, object],
        *,
        expected_package_lock_hash: str | None = None,
    ) -> dict[str, object]:
        calls.append((advertisement, expected_package_lock_hash))
        return {"ok": True, "advertisement": advertisement, "expected": expected_package_lock_hash}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(validate_worker_advertisement=validate_worker_advertisement),
    )
    advertisement = graphblocks_worker.WorkerAdvertisement.new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [graphblocks_worker.BlockCapability("document.parse@1")],
    )

    result = graphblocks_worker.validate_worker_advertisement_native(
        advertisement,
        expected_package_lock_hash="sha256:package-lock",
    )
    mapping_result = graphblocks_worker.validate_worker_advertisement_native(advertisement.to_wire())

    assert result["ok"] is True
    assert mapping_result["ok"] is True
    assert calls == [
        (advertisement.to_wire(), "sha256:package-lock"),
        (advertisement.to_wire(), None),
    ]
    assert "validate_worker_advertisement_native" in graphblocks_worker.__all__


def test_worker_package_native_remote_payload_helper_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-worker" / "src"))
    graphblocks_worker = importlib.import_module("graphblocks_worker")
    calls: list[tuple[dict[str, object], int]] = []

    def validate_remote_payload(payload: dict[str, object], *, max_inline_bytes: int) -> dict[str, object]:
        calls.append((payload, max_inline_bytes))
        return {"ok": True, "payload": payload, "maxInlineBytes": max_inline_bytes}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(validate_remote_payload=validate_remote_payload),
    )
    payload = graphblocks_worker.RemoteEdgePayload.artifact_ref(
        "graphblocks.ai/PdfDocument@1",
        artifact_id="artifact-1",
        uri="s3://graphblocks/documents/source.pdf",
    )

    result = graphblocks_worker.validate_remote_payload_native(payload, max_inline_bytes=8)
    mapping_result = graphblocks_worker.validate_remote_payload_native(payload.to_wire(), max_inline_bytes=16)

    assert result["ok"] is True
    assert mapping_result["ok"] is True
    assert calls == [(payload.to_wire(), 8), (payload.to_wire(), 16)]
    assert "validate_remote_payload_native" in graphblocks_worker.__all__


def test_worker_package_native_admission_helper_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-worker" / "src"))
    graphblocks_worker = importlib.import_module("graphblocks_worker")
    calls: list[tuple[dict[str, object], dict[str, object] | None, str, int]] = []

    def admit_worker_message(
        message: dict[str, object],
        *,
        daemon_config: dict[str, object] | None = None,
        response_message_id: str = "message-daemon-1",
        response_sequence: int = 1,
    ) -> dict[str, object]:
        calls.append((message, daemon_config, response_message_id, response_sequence))
        return {
            "ok": True,
            "response": {"kind": "admission_decision"},
            "message": message,
            "daemonConfig": daemon_config,
            "responseMessageId": response_message_id,
            "responseSequence": response_sequence,
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(admit_worker_message=admit_worker_message),
    )
    advertisement = graphblocks_worker.WorkerAdvertisement.new(
        "worker-1",
        "doc-cpu",
        "sha256:package-lock",
        "sha256:image",
        [graphblocks_worker.BlockCapability("document.parse@1")],
    )
    message = graphblocks_worker.WorkerProtocolMessage.advertisement(
        "message-worker-1",
        1,
        advertisement,
        correlation_id="worker-1",
    )

    result = graphblocks_worker.admit_worker_message_native(
        message,
        daemon_config={"daemonId": "daemon-1"},
        response_message_id="message-daemon-1",
        response_sequence=2,
    )
    mapping_result = graphblocks_worker.admit_worker_message_native(message.to_wire())

    assert result["response"] == {"kind": "admission_decision"}
    assert mapping_result["response"] == {"kind": "admission_decision"}
    assert calls == [
        (message.to_wire(), {"daemonId": "daemon-1"}, "message-daemon-1", 2),
        (message.to_wire(), None, "message-daemon-1", 1),
    ]
    assert "admit_worker_message_native" in graphblocks_worker.__all__
