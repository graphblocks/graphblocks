from __future__ import annotations

import importlib
import json
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
                "observed": {"hash": expected_hash, "ok": True, "error_codes": [], "warning_codes": []},
            }
        ],
    }
    assert report.content_digest().startswith("sha256:")


def test_testing_package_loads_shared_compiler_tck_cases_with_diagnostic_expectations(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_compiler_tck_cases(ROOT / "tck" / "compiler" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert len(cases) >= 20
    assert report.ok
    assert all("error_codes" in result.observed for result in report.results)
    assert any(result.observed["error_codes"] for result in report.results)
    assert "load_compiler_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_runtime_tck_cases_with_terminal_expectations(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_runtime_tck_cases(ROOT / "tck" / "runtime" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["runtime", "runtime"]
    assert any(case.expected_terminal_kind == "run_failed" for case in cases)
    assert report.ok
    assert {result.observed["terminal_kind"] for result in report.results} == {
        "run_failed",
        "run_succeeded",
    }
    assert "load_runtime_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_schema_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_schema_tck_cases(ROOT / "tck" / "schema" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["schema"] * 4
    assert any(not case.expected_ok for case in cases)
    assert report.ok
    assert {result.observed["valid"] for result in report.results} == {False, True}
    assert "load_schema_tck_cases" in graphblocks_testing.__all__


def test_testing_package_tck_loaders_accept_camel_case_aliases(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    compiler_graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "compiler-alias"},
        "spec": {"nodes": {"source": {"block": "prompt.render@1"}}},
    }
    compiler_cases = tmp_path / "compiler.json"
    compiler_cases.write_text(
        json.dumps(
            [
                {
                    "caseId": "compiler/alias",
                    "graph": compiler_graph,
                    "expected": {
                        "graphHash": graphblocks_testing.compile_graph(compiler_graph).graph_hash,
                        "errorCodes": [],
                        "warningCodes": [],
                    },
                    "blockCatalog": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    compiler_case = graphblocks_testing.load_compiler_tck_cases(compiler_cases)[0]

    assert compiler_case.case_id == "compiler/alias"
    assert compiler_case.expected_error_codes == ()

    runtime_cases = tmp_path / "runtime.json"
    runtime_cases.write_text(
        json.dumps(
            [
                {
                    "caseId": "runtime/alias",
                    "graph": {
                        "apiVersion": "graphblocks.ai/v1alpha3",
                        "kind": "Graph",
                        "metadata": {"name": "runtime-alias"},
                        "spec": {
                            "nodes": {
                                "render": {
                                    "block": "prompt.render@1",
                                    "config": {"template": "Hi {message.text}"},
                                    "inputs": {"message": "$input.message"},
                                    "outputs": {"prompt": "$output.prompt"},
                                }
                            }
                        },
                    },
                    "inputs": {"message": {"text": "Ada"}},
                    "expected": {
                        "expectedStatus": "succeeded",
                        "expectedOutputs": {"prompt": "Hi Ada"},
                        "expectedTerminalKind": "run_succeeded",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    runtime_case = graphblocks_testing.load_runtime_tck_cases(runtime_cases)[0]

    assert runtime_case.case_id == "runtime/alias"
    assert runtime_case.expected_outputs == {"prompt": "Hi Ada"}
    assert runtime_case.expected_terminal_kind == "run_succeeded"

    schema_cases = tmp_path / "schema.json"
    schema_cases.write_text(
        json.dumps(
            [
                {
                    "caseId": "schema/alias",
                    "schemaId": "schemas/Alias@2",
                    "expected": {
                        "valid": True,
                        "canonicalSchemaId": "schemas/Alias@2",
                        "schemaName": "schemas/Alias",
                        "majorVersion": 2,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    schema_case = graphblocks_testing.load_schema_tck_cases(schema_cases)[0]

    assert schema_case.case_id == "schema/alias"
    assert schema_case.expected_canonical_schema_id == "schemas/Alias@2"
    assert schema_case.expected_schema_name == "schemas/Alias"
    assert schema_case.expected_major_version == 2


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


def test_testing_package_builds_performance_benchmark_report(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    report = graphblocks_testing.PerformanceBenchmarkReport(
        benchmark_id="release-candidate",
        measurements={
            "throughput_rps": 55,
            "p95_latency_ms": 820,
        },
        thresholds=(
            graphblocks_testing.PerformanceThreshold.at_most("p95_latency_ms", 800, unit="ms"),
            graphblocks_testing.PerformanceThreshold.at_least("throughput_rps", 50, unit="rps"),
            graphblocks_testing.PerformanceThreshold.at_most("first_token_ms", 300, unit="ms"),
        ),
        metadata={"release": "2026.06.23.1"},
    )

    assert not report.ok
    assert report.report_contract() == {
        "benchmark_id": "release-candidate",
        "ok": False,
        "metadata": {"release": "2026.06.23.1"},
        "measurements": {
            "p95_latency_ms": 820.0,
            "throughput_rps": 55.0,
        },
        "thresholds": [
            {
                "metric_name": "first_token_ms",
                "operator": "at_most",
                "threshold": 300.0,
                "unit": "ms",
            },
            {
                "metric_name": "p95_latency_ms",
                "operator": "at_most",
                "threshold": 800.0,
                "unit": "ms",
            },
            {
                "metric_name": "throughput_rps",
                "operator": "at_least",
                "threshold": 50.0,
                "unit": "rps",
            },
        ],
        "issues": [
            {
                "metric_name": "first_token_ms",
                "observed": None,
                "operator": "at_most",
                "threshold": 300.0,
                "unit": "ms",
                "reason": "measurement_missing",
            },
            {
                "metric_name": "p95_latency_ms",
                "observed": 820.0,
                "operator": "at_most",
                "threshold": 800.0,
                "unit": "ms",
                "reason": "threshold_failed",
            },
        ],
    }
    assert report.content_digest().startswith("sha256:")
    assert "PerformanceBenchmarkReport" in graphblocks_testing.__all__
    assert "PerformanceThreshold" in graphblocks_testing.__all__


def test_testing_package_runs_migration_compatibility_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    legacy = {
        "apiVersion": "graphblocks.ai/v1alpha2",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}},
    }
    migrated = graphblocks_testing.migrate_document(legacy)
    expected_hash = graphblocks_testing.canonical_hash(migrated)

    report = graphblocks_testing.MigrationCompatibilityRunner().run_cases(
        (
            graphblocks_testing.MigrationCompatibilityCase.upgrade(
                case_id="legacy-alpha2",
                document=legacy,
                expected_hash=expected_hash,
            ),
            graphblocks_testing.MigrationCompatibilityCase.upgrade(
                case_id="hash-mismatch",
                document=legacy,
                expected_hash="sha256:wrong",
            ),
        )
    )

    assert not report.ok
    assert report.report_contract() == {
        "profile": "migration",
        "ok": False,
        "results": [
            {
                "case_id": "hash-mismatch",
                "direction": "upgrade",
                "status": "failed",
                "diagnostics": [
                    {
                        "code": "MigrationHashMismatch",
                        "message": "migrated document hash did not match expected hash",
                        "path": "$.expected_hash",
                    }
                ],
                "observed": {
                    "api_version": "graphblocks.ai/v1alpha3",
                    "graph_hash": expected_hash,
                    "migrated_from": "graphblocks.ai/v1alpha2",
                    "source_mutated": False,
                },
            },
            {
                "case_id": "legacy-alpha2",
                "direction": "upgrade",
                "status": "passed",
                "diagnostics": [],
                "observed": {
                    "api_version": "graphblocks.ai/v1alpha3",
                    "graph_hash": expected_hash,
                    "migrated_from": "graphblocks.ai/v1alpha2",
                    "source_mutated": False,
                },
            },
        ],
    }
    assert report.content_digest().startswith("sha256:")
    assert "MigrationCompatibilityRunner" in graphblocks_testing.__all__


def test_testing_package_builds_fault_chaos_report(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    report = graphblocks_testing.FaultChaosReport(
        profile="release-candidate",
        results=(
            graphblocks_testing.FaultChaosResult.from_observation(
                case_id="telemetry-outage",
                fault_kind="telemetry_outage",
                expected_terminal_state="succeeded",
                observed_terminal_state="succeeded",
                recovery_expected=True,
                recovered=True,
                data_loss_events=0,
                audit_preserved=True,
            ),
            graphblocks_testing.FaultChaosResult.from_observation(
                case_id="worker-crash",
                fault_kind="worker_crash",
                expected_terminal_state="succeeded",
                observed_terminal_state="failed",
                recovery_expected=True,
                recovered=False,
                data_loss_events=1,
                audit_preserved=False,
            ),
        ),
    )

    assert not report.ok
    assert report.report_contract() == {
        "profile": "release-candidate",
        "ok": False,
        "results": [
            {
                "case_id": "telemetry-outage",
                "fault_kind": "telemetry_outage",
                "status": "passed",
                "diagnostics": [],
                "observed": {
                    "audit_preserved": True,
                    "data_loss_events": 0,
                    "expected_terminal_state": "succeeded",
                    "observed_terminal_state": "succeeded",
                    "recovered": True,
                    "recovery_expected": True,
                },
            },
            {
                "case_id": "worker-crash",
                "fault_kind": "worker_crash",
                "status": "failed",
                "diagnostics": [
                    {
                        "code": "ChaosTerminalStateMismatch",
                        "message": "fault scenario terminal state did not match expected state",
                        "path": "$.observed_terminal_state",
                    },
                    {
                        "code": "ChaosRecoveryFailed",
                        "message": "fault scenario did not recover as expected",
                        "path": "$.recovered",
                    },
                    {
                        "code": "ChaosDataLossObserved",
                        "message": "fault scenario observed data loss events",
                        "path": "$.data_loss_events",
                    },
                    {
                        "code": "ChaosAuditNotPreserved",
                        "message": "fault scenario did not preserve audit evidence",
                        "path": "$.audit_preserved",
                    },
                ],
                "observed": {
                    "audit_preserved": False,
                    "data_loss_events": 1,
                    "expected_terminal_state": "succeeded",
                    "observed_terminal_state": "failed",
                    "recovered": False,
                    "recovery_expected": True,
                },
            },
        ],
    }
    assert report.content_digest().startswith("sha256:")
    assert "FaultChaosReport" in graphblocks_testing.__all__


def test_testing_package_builds_release_candidate_gate_report(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    passing_tck = graphblocks_testing.TckReport(
        profile="compiler",
        results=(graphblocks_testing.TckResult("compiler/hash", "compiler", "passed"),),
    )
    failing_performance = graphblocks_testing.PerformanceBenchmarkReport(
        benchmark_id="release-candidate",
        measurements={"p95_latency_ms": 900},
        thresholds=(graphblocks_testing.PerformanceThreshold.at_most("p95_latency_ms", 800, unit="ms"),),
    )
    fault_chaos = graphblocks_testing.FaultChaosReport(
        profile="release-candidate",
        results=(
            graphblocks_testing.FaultChaosResult.from_observation(
                case_id="telemetry-outage",
                fault_kind="telemetry_outage",
                expected_terminal_state="succeeded",
                observed_terminal_state="succeeded",
                recovery_expected=True,
                recovered=True,
                data_loss_events=0,
                audit_preserved=True,
            ),
        ),
    )
    migration = graphblocks_testing.MigrationCompatibilityReport(
        profile="migration",
        results=(
            graphblocks_testing.MigrationCompatibilityResult(
                case_id="legacy-alpha2",
                direction="upgrade",
                status="passed",
            ),
        ),
    )
    wheel_matrix = graphblocks.WheelMatrix(
        targets=(
            graphblocks.WheelBuildTarget(
                distribution="graphblocks-core",
                manifest="pyproject.toml",
                backend="hatchling.build",
                kind="pure_python",
                source_layout="src/graphblocks",
                python_versions=("3.11", "3.12"),
            ),
        )
    )
    oci_image_build = graphblocks_testing.ReleaseCandidateEvidence(
        evidence_id="oci-image-build",
        ok=True,
        digest="sha256:oci-image-build",
    )

    report = graphblocks_testing.ReleaseCandidateGateReport.from_evidence(
        release_id="2026.06.23.1",
        tck_reports={"compiler": passing_tck},
        required_tck_suites=("compiler", "runtime"),
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
        fault_chaos=fault_chaos,
        performance=failing_performance,
        wheel_matrix=wheel_matrix,
        migration=migration,
        oci_image_build=oci_image_build,
        supply_chain={
            "sbom": "sha256:sbom",
            "provenance": "sha256:provenance",
            "signature": "sha256:signature",
        },
    )

    assert not report.ok
    assert report.report_contract()["release_id"] == "2026.06.23.1"
    assert [gate["gate"] for gate in report.report_contract()["gates"]] == [
        "acceptance_applications",
        "fault_chaos_tests",
        "full_tck",
        "migration_tests",
        "oci_image_build",
        "performance_benchmark",
        "supply_chain",
        "wheel_matrix",
    ]
    failing = {gate["gate"]: gate for gate in report.report_contract()["gates"] if gate["status"] == "failed"}
    assert failing["full_tck"]["diagnostics"] == [
        {
            "code": "ReleaseCandidateTckMissing",
            "message": "required TCK suite has no report",
            "path": "$.tck_reports.runtime",
        }
    ]
    assert failing["performance_benchmark"]["diagnostics"] == [
        {
            "code": "ReleaseCandidatePerformanceFailed",
            "message": "performance benchmark did not pass",
            "path": "$.performance",
        }
    ]
    assert report.content_digest().startswith("sha256:")
    assert "ReleaseCandidateGateReport" in graphblocks_testing.__all__
