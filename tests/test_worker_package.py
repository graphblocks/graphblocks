from __future__ import annotations

import importlib
from pathlib import Path


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
    assert "WorkerDrainPlan" in graphblocks_worker.__all__
