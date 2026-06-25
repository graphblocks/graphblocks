from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_testing_package_exposes_deterministic_in_process_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "testing-runtime"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Test {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    result = graphblocks_testing.InProcessRuntime(graphblocks_testing.stdlib_registry()).run(
        graph,
        {"message": {"text": "ok"}},
    )

    assert result.status == "succeeded"
    assert result.outputs == {"prompt": "Test ok"}
    assert result.journal.terminal_kind == "run_succeeded"


def test_testing_package_runs_compiler_tck_case_and_reports_hash(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "compiler-case"},
        "spec": {"nodes": {"source": {"block": "prompt.render@1"}}},
    }
    expected_hash = graphblocks_testing.compile_graph(graph).graph_hash
    case = graphblocks_testing.TckCase.compiler(
        case_id="compiler/hash-stable",
        graph=graph,
        expected_hash=expected_hash,
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.report_contract() == {
        "profile": "local",
        "ok": True,
        "results": [
            {
                "case_id": "compiler/hash-stable",
                "kind": "compiler",
                "status": "passed",
                "diagnostics": [],
                "observed": {"hash": expected_hash, "ok": True},
            }
        ],
    }
    assert report.content_digest().startswith("sha256:")


def test_testing_package_runs_runtime_tck_case_and_reports_output_mismatch(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-case"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Hello {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    case = graphblocks_testing.TckCase.runtime(
        case_id="runtime/output-mismatch",
        graph=graph,
        inputs={"message": {"text": "Ada"}},
        expected_outputs={"prompt": "wrong"},
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].status == "failed"
    assert report.results[0].diagnostics == (
        {
            "code": "OutputMismatch",
            "message": "runtime outputs did not match expected outputs",
            "path": "$.expected_outputs",
        },
    )
    assert report.results[0].observed == {
        "status": "succeeded",
        "outputs": {"prompt": "Hello Ada"},
        "terminal_kind": "run_succeeded",
    }


def test_testing_package_exposes_terminal_run_store_error(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    store = graphblocks_testing.InMemoryRunStore()
    provenance = graphblocks_testing.RunDeploymentProvenance(
        release_digest="sha256:release",
        physical_plan_hash="sha256:physical",
    )
    record = store.create_run("sha256:test", {}, deployment_provenance=provenance)
    store.set_status(record.run_id, "succeeded")

    try:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)
    except graphblocks_testing.RunTerminalStateError as error:
        assert error.status == "succeeded"
        assert "RunDeploymentProvenance" in graphblocks_testing.__all__
    else:  # pragma: no cover - test should fail before this branch.
        raise AssertionError("terminal run state mutation was allowed")
