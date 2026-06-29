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

    assert [case.kind for case in cases] == ["runtime"] * 5
    assert {case.case_id for case in cases} >= {
        "control_map_renders_each_item",
        "control_select_treats_null_as_present",
        "tools_resolve_feeds_scripted_agent",
    }
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


def test_testing_package_loads_shared_policy_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_policy_tck_cases(ROOT / "tck" / "policy" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["policy"] * 9
    assert {case.case_id for case in cases} >= {
        "abort_turn_stops_delivery_and_marks_draft_incomplete",
        "deny_commit_stops_buffered_candidate_without_delivery",
    }
    assert report.ok
    assert any(result.observed["stopped"] for result in report.results)
    assert any(result.observed["lastClientDeliveredSequence"] == 2 for result in report.results)
    assert "load_policy_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_application_event_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_application_event_tck_cases(
        ROOT / "tck" / "application-events" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["application-events"] * 3
    assert report.ok
    assert {tuple(result.observed["accepted_kinds"]) for result in report.results} == {
        ("OutputCutoff", "AssistantRetracted", "RunSucceeded"),
        ("OutputCutoff", "AssistantIncomplete", "RunSucceeded"),
        ("ToolCallStarted", "ToolCallCompleted"),
    }
    assert "load_application_event_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_sequence_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_sequence_tck_cases(ROOT / "tck" / "sequence" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["sequence"] * 3
    assert report.ok
    assert {result.observed.get("state") for result in report.results if "state" in result.observed} == {
        "completed",
        "open",
    }
    assert any(result.observed.get("creation_error") == "invalid_capacity" for result in report.results)
    assert "load_sequence_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_exhaustion_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_exhaustion_tck_cases(ROOT / "tck" / "exhaustion" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["exhaustion"] * 5
    assert report.ok
    assert any(result.observed.get("validation_error") == "missing_exhaustion_boundary" for result in report.results)
    assert {result.observed.get("usedAdditionalSteps") for result in report.results} >= {0, 1, 2}
    assert "load_exhaustion_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_budget_race_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_budget_race_tck_cases(ROOT / "tck" / "budget-race" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["budget-race"] * 2
    assert report.ok
    assert {result.observed["allowed"] for result in report.results} == {1}
    assert {result.observed["denied"] for result in report.results} == {1}
    assert "load_budget_race_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_tool_lifecycle_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_tool_lifecycle_tck_cases(
        ROOT / "tck" / "tool-lifecycle" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["tool-lifecycle"] * 3
    assert report.ok
    assert {case.case_id for case in cases} == {
        "incremental_arguments_do_not_finalize_call",
        "invalid_arguments_denied_before_policy_admission",
        "approval_invalid_after_argument_mutation",
    }
    assert any(not result.observed.get("finalizedBeforeComplete", True) for result in report.results)
    assert any(result.observed.get("schemaRejectedBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("mutatedApprovalValid") is False for result in report.results)
    assert "load_tool_lifecycle_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_usage_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_usage_tck_cases(ROOT / "tck" / "usage" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["usage"] * 2
    assert report.ok
    assert {tuple(result.observed["recordIds"]) for result in report.results} == {
        ("usage-provisional", "usage-reconciled"),
        ("usage-provider-1",),
    }
    assert {tuple(result.observed["appendResults"]) for result in report.results} == {
        ("usage-provisional", "usage-reconciled"),
        ("usage-provider-1", "usage-provider-1"),
    }
    assert "load_usage_tck_cases" in graphblocks_testing.__all__


def test_testing_package_discovers_all_shared_tck_suite_manifests(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    manifests = graphblocks_testing.load_tck_suite_manifests(ROOT / "tck")
    by_suite = {manifest.suite_id: manifest for manifest in manifests}

    assert tuple(by_suite) == (
        "application-events",
        "budget-race",
        "compiler",
        "exhaustion",
        "policy",
        "runtime",
        "schema",
        "sequence",
        "tool-lifecycle",
        "usage",
    )
    assert by_suite["budget-race"].case_ids == (
        "competing_reservations_serialize_against_available_budget",
        "completion_reserve_allows_only_one_concurrent_spender",
    )
    assert by_suite["policy"].case_count >= 4
    assert by_suite["budget-race"].manifest_contract() == {
        "suite_id": "budget-race",
        "path": "budget-race/cases.json",
        "case_count": 2,
        "case_ids": [
            "competing_reservations_serialize_against_available_budget",
            "completion_reserve_allows_only_one_concurrent_spender",
        ],
    }
    assert by_suite["application-events"].case_ids == (
        "output_cutoff_discards_late_commit_for_same_response",
        "output_cutoff_marks_draft_incomplete",
        "tool_result_delta_is_draft_until_completed",
    )
    assert by_suite["tool-lifecycle"].case_ids == (
        "incremental_arguments_do_not_finalize_call",
        "invalid_arguments_denied_before_policy_admission",
        "approval_invalid_after_argument_mutation",
    )
    assert by_suite["budget-race"].content_digest().startswith("sha256:")
    assert "TckSuiteManifest" in graphblocks_testing.__all__
    assert "load_tck_suite_manifests" in graphblocks_testing.__all__


def test_testing_package_cli_lists_tck_suite_manifests(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    assert graphblocks_testing.main(["list", str(ROOT / "tck"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["suiteCount"] == 10
    assert payload["suites"][0]["suite_id"] == "application-events"
    assert payload["suites"][0]["case_count"] == 3
    assert payload["contentDigest"].startswith("sha256:")
    assert "main" in graphblocks_testing.__all__


def test_testing_package_cli_checks_tck_suite_coverage(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        [
            "check",
            str(ROOT / "tck"),
            "--profiles",
            str(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"),
            "--profile",
            "GB-C3-GOVERNED-RUNTIME",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["claim"]["tck_suites"] == [
        "application-events",
        "budget-race",
        "compiler",
        "exhaustion",
        "policy",
        "runtime",
        "schema",
        "sequence",
        "tool-lifecycle",
        "usage",
    ]
    assert payload["missing_suites"] == []
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_policy_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "policy", str(ROOT / "tck" / "policy" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile"] == "local"
    assert {result["kind"] for result in payload["results"]} == {"policy"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_application_event_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        [
            "run",
            "application-events",
            str(ROOT / "tck" / "application-events" / "cases.json"),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"application-events"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_sequence_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "sequence", str(ROOT / "tck" / "sequence" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"sequence"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_exhaustion_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "exhaustion", str(ROOT / "tck" / "exhaustion" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"exhaustion"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_budget_race_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "budget-race", str(ROOT / "tck" / "budget-race" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"budget-race"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_tool_lifecycle_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "tool-lifecycle", str(ROOT / "tck" / "tool-lifecycle" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"tool-lifecycle"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_usage_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "usage", str(ROOT / "tck" / "usage" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"usage"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_all_supported_tck_suites(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(["run-all", str(ROOT / "tck"), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert tuple(payload["reports"]) == (
        "application-events",
        "budget-race",
        "compiler",
        "exhaustion",
        "policy",
        "runtime",
        "schema",
        "sequence",
        "tool-lifecycle",
        "usage",
    )
    assert all(report["ok"] for report in payload["reports"].values())
    assert payload["contentDigest"].startswith("sha256:")


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
