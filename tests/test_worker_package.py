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
