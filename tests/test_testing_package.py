from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


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


def test_testing_package_lazy_native_runner_delegates_to_runtime(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    calls: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []

    def run_test_graph(
        graph: dict[str, object],
        inputs: dict[str, object],
        node_outputs: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        calls.append((graph, inputs, node_outputs, options))
        return {
            "runId": options["run_id"],
            "status": "succeeded",
            "graph": graph,
            "inputs": inputs,
            "nodeOutputs": node_outputs,
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_test_graph=run_test_graph),
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    result = graphblocks_testing.run_native_test_graph(
        {"kind": "Graph", "metadata": {"name": "test"}},
        {"message": "hi"},
        {"render": {"prompt": "Hello"}},
        run_id="test-run-requested-1",
        run_store_path="/tmp/graphblocks-test-run.sqlite3",
        journal_store_path="/tmp/graphblocks-test-journal.sqlite3",
    )

    assert result == {
        "runId": "test-run-requested-1",
        "status": "succeeded",
        "graph": {"kind": "Graph", "metadata": {"name": "test"}},
        "inputs": {"message": "hi"},
        "nodeOutputs": {"render": {"prompt": "Hello"}},
    }
    assert calls == [
        (
            {"kind": "Graph", "metadata": {"name": "test"}},
            {"message": "hi"},
            {"render": {"prompt": "Hello"}},
            {
                "journal_store_path": "/tmp/graphblocks-test-journal.sqlite3",
                "run_id": "test-run-requested-1",
                "run_store_path": "/tmp/graphblocks-test-run.sqlite3",
            },
        )
    ]
    assert "run_native_test_graph" in graphblocks_testing.__all__


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

    assert len(cases) >= 7
    assert all(case.kind == "runtime" for case in cases)
    assert {case.case_id for case in cases} >= {
        "control_map_renders_each_item",
        "control_select_treats_null_as_present",
        "tools_resolve_feeds_scripted_agent",
        "tools_resolve_rejects_blank_scope_tool_name",
        "tools_resolve_rejects_non_string_definition_tag",
    }
    assert any(case.expected_terminal_kind == "run_failed" for case in cases)
    assert sum(1 for case in cases if case.native_node_outputs) == 4
    assert report.ok
    assert {result.observed["terminal_kind"] for result in report.results} == {
        "run_failed",
        "run_succeeded",
    }
    assert "load_runtime_tck_cases" in graphblocks_testing.__all__


def test_testing_package_native_profile_runs_runtime_tck_case_through_native_bridge(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    calls: list[tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]] = []

    def run_test_graph(
        graph: dict[str, object],
        inputs: dict[str, object],
        node_outputs: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        calls.append((graph, inputs, node_outputs, options))
        return {
            "runId": options["run_id"],
            "status": "succeeded",
            "outputs": {"prompt": "Native Ada"},
            "journal": [
                {"kind": "run_started", "runId": options["run_id"]},
                {"kind": "node_started", "runId": options["run_id"], "nodeId": "render"},
                {"kind": "node_completed", "runId": options["run_id"], "nodeId": "render"},
                {"kind": "run_succeeded", "runId": options["run_id"], "terminal": True},
            ],
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_test_graph=run_test_graph),
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "native-runtime-tck"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    case = graphblocks_testing.TckCase.runtime(
        case_id="runtime/native-profile",
        graph=graph,
        inputs={"message": {"text": "Ada"}},
        native_node_outputs={"render": {"prompt": "Native Ada"}},
        expected_outputs={"prompt": "Native Ada"},
        expected_terminal_kind="run_succeeded",
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry(), profile="native").run_cases((case,))

    assert report.ok
    assert report.results[0].observed == {
        "status": "succeeded",
        "outputs": {"prompt": "Native Ada"},
        "terminal_kind": "run_succeeded",
        "run_id": "tck-runtime-native-profile",
        "runtime": "native",
        "journal_kinds": ["run_started", "node_started", "node_completed", "run_succeeded"],
    }
    assert calls == [
        (
            graph,
            {"message": {"text": "Ada"}},
            {"render": {"prompt": "Native Ada"}},
            {"run_id": "tck-runtime-native-profile"},
        )
    ]


def test_testing_package_native_profile_falls_back_for_unannotated_runtime_case(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "native-runtime-fallback"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Fallback {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    case = graphblocks_testing.TckCase.runtime(
        case_id="runtime/native-fallback",
        graph=graph,
        inputs={"message": {"text": "Ada"}},
        expected_outputs={"prompt": "Fallback Ada"},
        expected_terminal_kind="run_succeeded",
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry(), profile="native").run_cases((case,))

    assert report.ok
    assert report.results[0].observed == {
        "status": "succeeded",
        "outputs": {"prompt": "Fallback Ada"},
        "terminal_kind": "run_succeeded",
        "runtime": "local",
        "native_fallback_reason": "missing_native_node_outputs",
    }


def test_testing_package_native_profile_falls_back_when_native_runtime_unavailable(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))

    def run_test_graph(*_args: object, **_options: object) -> dict[str, object]:
        raise ModuleNotFoundError("No module named 'graphblocks_runtime'")

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_test_graph=run_test_graph),
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "native-runtime-unavailable"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Unavailable {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    case = graphblocks_testing.TckCase.runtime(
        case_id="runtime/native-unavailable",
        graph=graph,
        inputs={"message": {"text": "Ada"}},
        native_node_outputs={"render": {"prompt": "Native Ada"}},
        expected_outputs={"prompt": "Unavailable Ada"},
        expected_terminal_kind="run_succeeded",
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry(), profile="native").run_cases((case,))

    assert report.ok
    assert report.results[0].observed == {
        "status": "succeeded",
        "outputs": {"prompt": "Unavailable Ada"},
        "terminal_kind": "run_succeeded",
        "runtime": "local",
        "native_fallback_reason": "native_runtime_unavailable",
    }


def test_testing_package_loads_runtime_tck_native_node_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    cases_path = tmp_path / "runtime-native-cases.json"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "native-runtime-loader"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    cases_path.write_text(
        json.dumps(
            [
                {
                    "name": "runtime/native-loader",
                    "document": graph,
                    "inputs": {"message": {"text": "Ada"}},
                    "nativeNodeOutputs": {"render": {"prompt": "Native Ada"}},
                    "expected": {
                        "status": "succeeded",
                        "outputs": {"prompt": "Native Ada"},
                        "terminalKind": "run_succeeded",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    cases = graphblocks_testing.load_runtime_tck_cases(cases_path)

    assert cases[0].native_node_outputs == {"render": {"prompt": "Native Ada"}}


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


def test_testing_package_rejects_non_standard_tck_json_constants(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        '[{"name":"schema/non-standard","schema_id":"schemas/Message@1",'
        '"expected":{"valid":true},"ignored":NaN}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema TCK cases must be valid strict JSON"):
        graphblocks_testing.load_schema_tck_cases(cases_path)


def test_testing_package_loads_shared_typed_value_schema_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_schema_typed_value_tck_cases(
        ROOT / "tck" / "schema" / "typed-values.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["schema"] * 3
    assert any(not case.expected_ok for case in cases)
    assert report.ok
    assert {result.observed["valid"] for result in report.results} == {False, True}
    assert any(
        result.observed.get("canonical_json") == '{"schema":"schemas/Message@1","value":{"a":[true],"z":1}}'
        for result in report.results
    )
    assert "load_schema_typed_value_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_policy_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_policy_tck_cases(ROOT / "tck" / "policy" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["policy"] * 14
    assert {case.case_id for case in cases} >= {
        "bounded_holdback_flushes_only_at_sentence_boundary",
        "abort_turn_stops_delivery_and_marks_draft_incomplete",
        "deny_commit_stops_buffered_candidate_without_delivery",
        "decision_rejects_zero_evaluated_at",
        "decision_rejects_zero_occurred_at",
        "hold_keeps_pending_output_until_later_allow",
        "buffer_until_commit_exposes_no_rejected_content",
    }
    assert report.ok
    assert any(result.observed["stopped"] for result in report.results)
    assert any(result.observed["lastClientDeliveredSequence"] == 2 for result in report.results)
    assert "load_policy_tck_cases" in graphblocks_testing.__all__


def test_testing_package_policy_tck_maps_invalid_generation_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.policy(
        case_id="policy/invalid-generation-sequence",
        delivery={
            "mode": "bounded_holdback",
            "holdbackMaxTokens": 8,
            "onViolation": "abort_response",
        },
        operations=(
            {
                "op": "chunk",
                "sequence": 0,
                "text": "invalid",
                "expectError": "invalid_generation_sequence",
            },
        ),
        expected={
            "lastGeneratedSequence": 0,
            "lastPolicyAcceptedSequence": 0,
            "lastClientDeliveredSequence": 0,
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].diagnostics == ()
    assert report.results[0].observed["lastGeneratedSequence"] == 0


def test_testing_package_loads_shared_application_event_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_application_event_tck_cases(
        ROOT / "tck" / "application-events" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["application-events"] * 8
    assert report.ok
    assert {tuple(result.observed["accepted_kinds"]) for result in report.results} == {
        (
            "ToolCallValidated",
            "ToolCallAdmitted",
            "ToolCallStarted",
            "ToolCallCompleted",
            "ToolCallFailed",
            "ToolCallDenied",
            "ToolCallCancelled",
            "ToolCallPolicyStopped",
            "ToolCallIncomplete",
        ),
        ("OutputCutoff", "AssistantRetracted", "RunSucceeded"),
        ("OutputCutoff", "AssistantIncomplete", "RunSucceeded"),
        (
            "OutputPolicyEvaluationStarted",
            "OutputPolicyHeld",
            "OutputPolicyRedacted",
            "OutputPolicyViolationDetected",
        ),
        ("ToolResultStarted", "ToolResultDelta", "ToolResultArtifactReady", "ToolResultCompleted"),
        (
            "ToolResultStarted",
            "ToolResultDelta",
            "ToolResultCancelled",
            "ToolResultStarted",
            "ToolResultDelta",
            "ToolResultPolicyStopped",
            "ToolResultStarted",
            "ToolResultDelta",
            "ToolResultIncomplete",
        ),
        (
            "ToolResultStarted",
            "ToolResultDelta",
            "ToolResultFailed",
            "ToolResultDenied",
        ),
        ("RunSucceeded", "RunSucceeded", "RunSucceeded"),
    }
    assert "load_application_event_tck_cases" in graphblocks_testing.__all__


def test_testing_package_application_event_tck_preserves_authoritative_event_metadata(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_events(
        case_id="application-events/authoritative-event-metadata",
        operations=(
            {
                "op": "run_succeeded",
                "runId": "run-metadata",
                "responseId": "response-metadata",
                "turnId": "turn-metadata",
                "cursor": "evt_000123",
                "releaseId": "release-metadata",
                "policySnapshotId": "policy-metadata",
                "graphId": "graph-metadata",
                "nodeId": "node-metadata",
                "operationId": "operation-metadata",
                "visibility": "operator",
            },
        ),
        expected_accepted_kinds=("RunSucceeded",),
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["accepted_metadata"] == [
        {
            "event_id": "application-events/authoritative-event-metadata:1",
            "run_id": "run-metadata",
            "response_id": "response-metadata",
            "turn_id": "turn-metadata",
            "sequence": 1,
            "cursor": "evt_000123",
            "release_id": "release-metadata",
            "policy_snapshot_id": "policy-metadata",
            "occurred_at": "2026-06-23T00:00:00Z",
            "graph_id": "graph-metadata",
            "node_id": "node-metadata",
            "operation_id": "operation-metadata",
            "visibility": "operator",
        }
    ]


def test_testing_package_application_event_tck_rejects_boolean_tool_result_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_events(
        case_id="application-events/boolean-tool-result-sequence",
        operations=(
            {
                "op": "tool_result_delta",
                "toolCallId": "call-1",
                "toolResultSequence": True,
                "output": [{"kind": "text", "text": "draft"}],
            },
        ),
        expected_accepted_kinds=(),
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics[0]["code"] == "ApplicationEventToolResultSequenceInvalid"
    assert report.results[0].observed["accepted_kinds"] == []


def test_testing_package_application_event_tck_rejects_boolean_generation_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_events(
        case_id="application-events/boolean-generation-sequence",
        operations=(
            {
                "op": "output_policy_evaluation_started",
                "sequence": True,
                "text": "draft",
                "inputDigest": "sha256:boolean-generation",
            },
        ),
        expected_accepted_kinds=(),
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics[0]["code"] == "ApplicationEventGenerationSequenceInvalid"
    assert report.results[0].observed["accepted_kinds"] == []


def test_testing_package_application_event_tck_rejects_boolean_policy_acceptance(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_events(
        case_id="application-events/boolean-policy-accepted-through",
        operations=(
            {
                "op": "output_policy_decision",
                "disposition": "allow",
                "decisionId": "decision-1",
                "inputDigest": "sha256:boolean-accepted-through",
                "acceptedThrough": True,
            },
        ),
        expected_accepted_kinds=(),
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics[0]["code"] == "ApplicationEventPolicyAcceptedThroughInvalid"
    assert report.results[0].observed["accepted_kinds"] == []


def test_testing_package_application_event_tck_rejects_boolean_output_cutoff_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_events(
        case_id="application-events/boolean-output-cutoff-sequence",
        operations=(
            {
                "op": "output_cutoff",
                "lastGeneratedSequence": True,
                "lastPolicyAcceptedSequence": 0,
                "lastClientDeliveredSequence": 0,
            },
        ),
        expected_accepted_kinds=(),
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics[0]["code"] == "ApplicationEventOutputCutoffInvalid"
    assert report.results[0].observed["accepted_kinds"] == []


def test_testing_package_loads_shared_application_protocol_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_application_protocol_tck_cases(
        ROOT / "tck" / "application-protocol" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["application-protocol"] * 12
    assert report.ok
    assert {case.case_id for case in cases} == {
        "application_protocol_kind_sets_match_contract",
        "command_envelope_preserves_metadata_and_payload",
        "event_envelope_accepts_output_cutoff_event",
        "event_envelope_preserves_async_operation_metadata",
        "command_envelope_rejects_non_object_payload",
        "event_envelope_rejects_non_object_payload",
        "capability_negotiation_intersects_commands_and_events",
        "capability_negotiation_rejects_blank_protocol_version",
        "protocol_log_suppresses_duplicates_and_replays_after_cursor",
        "protocol_log_rejects_events_from_another_run",
        "protocol_log_rejects_mutated_duplicate_event_ids",
        "protocol_stream_cutoff_discards_late_output",
    }
    assert any("OutputCutoff" in result.observed.get("events", []) for result in report.results)
    assert any(
        result.case_id == "event_envelope_preserves_async_operation_metadata"
        and result.observed.get("operationId") == "operation-ci-1"
        for result in report.results
    )
    assert {result.observed.get("error") for result in report.results} >= {
        "invalid_payload",
        "empty_protocol_version",
    }
    assert "load_application_protocol_tck_cases" in graphblocks_testing.__all__


def test_testing_package_application_protocol_tck_rejects_boolean_command_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_protocol(
        case_id="application-protocol/boolean-command-sequence",
        fixture={
            "kind": "command_envelope_error",
            "commandKind": "ApproveEffect",
            "metadata": {
                "commandId": "command-bool-sequence",
                "protocolVersion": "graphblocks.app.v1",
                "runId": "run-1",
                "sequence": True,
                "issuedAtUnixMs": 1765843200000,
            },
            "payload": {"tool_call_id": "tool-call-1"},
            "expected": {"error": "application command sequence must be an integer"},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["error"] == "application command sequence must be an integer"


def test_testing_package_application_protocol_tck_rejects_boolean_event_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_protocol(
        case_id="application-protocol/boolean-event-sequence",
        fixture={
            "kind": "event_envelope_error",
            "eventKind": "AssistantDraftDelta",
            "metadata": {
                "eventId": "event-bool-sequence",
                "protocolVersion": "graphblocks.app.v1",
                "runId": "run-1",
                "sequence": True,
                "cursor": "cursor-bool-sequence",
                "occurredAtUnixMs": 1765843201000,
            },
            "payload": {"text": "draft"},
            "expected": {"error": "application event sequence must be an integer"},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["error"] == "application event sequence must be an integer"


def test_testing_package_application_protocol_tck_rejects_boolean_log_event_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_protocol(
        case_id="application-protocol/boolean-log-event-sequence",
        fixture={
            "kind": "protocol_log",
            "operations": [
                {
                    "eventKind": "JobProgress",
                    "metadata": {
                        "eventId": "event-log-bool-sequence",
                        "protocolVersion": "graphblocks.app.v1",
                        "runId": "run-1",
                        "sequence": True,
                        "cursor": "cursor-log-bool-sequence",
                        "occurredAtUnixMs": 1765843201000,
                    },
                    "payload": {"done": 1, "total": 1},
                    "expectError": "application event sequence must be an integer",
                }
            ],
            "expected": {
                "eventIds": [],
                "appendResults": [False],
                "appendErrors": ["application event sequence must be an integer"],
                "replayEventIds": [],
                "length": 0,
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["appendErrors"] == [
        "application event sequence must be an integer"
    ]


def test_testing_package_application_protocol_tck_rejects_boolean_stream_cutoff_sequence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_protocol(
        case_id="application-protocol/boolean-stream-cutoff-sequence",
        fixture={
            "kind": "stream_cutoff",
            "operations": [
                {
                    "eventKind": "AssistantDraftDelta",
                    "metadata": {
                        "eventId": "event-stream-bool-sequence",
                        "protocolVersion": "graphblocks.app.v1",
                        "runId": "run-1",
                        "sequence": True,
                        "cursor": "cursor-stream-bool-sequence",
                        "occurredAtUnixMs": 1765843201000,
                    },
                    "payload": {"response_id": "response-1", "chunk_sequence": 1},
                    "expectError": "application event sequence must be an integer",
                }
            ],
            "expected": {
                "acceptedKinds": [],
                "cutoffResponseId": None,
                "cutoffLastClientDeliveredSequence": None,
                "errors": ["application event sequence must be an integer"],
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["errors"] == ["application event sequence must be an integer"]


def test_testing_package_application_protocol_tck_rejects_boolean_replay_limit(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.application_protocol(
        case_id="application-protocol/boolean-replay-limit",
        fixture={
            "kind": "protocol_log",
            "operations": [
                {
                    "eventKind": "RunStarted",
                    "metadata": {
                        "eventId": "event-replay-limit",
                        "protocolVersion": "graphblocks.app.v1",
                        "runId": "run-1",
                        "sequence": 1,
                        "cursor": "cursor-1",
                        "occurredAtUnixMs": 1765843201000,
                    },
                    "payload": {},
                    "expectAppended": True,
                }
            ],
            "replayLimit": True,
            "expected": {
                "eventIds": ["event-replay-limit"],
                "appendResults": [True],
                "appendErrors": [],
                "replayEventIds": [],
                "length": 1,
                "replayError": "application protocol replay limit must be an integer",
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["replayError"] == (
        "application protocol replay limit must be an integer"
    )


def test_testing_package_loads_shared_approval_review_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_approval_review_tck_cases(
        ROOT / "tck" / "approval-review" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["approval-review"] * 6
    assert report.ok
    assert {case.case_id for case in cases} == {
        "review_request_digest_is_scope_order_invariant",
        "credentialed_review_completes_required_scope",
        "changed_review_subject_is_rejected",
        "invalidated_review_does_not_complete_scope",
        "missing_reviewer_credential_is_rejected",
        "expired_reviewer_credential_is_rejected",
    }
    assert any(result.observed.get("complete") is True for result in report.results)
    assert any(result.observed.get("error") == "review_subject_changed" for result in report.results)
    assert any(result.observed.get("error") == "review_credential_missing" for result in report.results)
    assert "load_approval_review_tck_cases" in graphblocks_testing.__all__


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

    assert [case.kind for case in cases] == ["exhaustion"] * 7
    assert report.ok
    assert {case.case_id for case in cases} >= {
        "continuation_permit_profile_mismatch_is_denied",
        "continuation_usage_accumulates_against_envelope",
    }
    assert any(result.observed.get("validation_error") == "missing_exhaustion_boundary" for result in report.results)
    assert {result.observed.get("usedAdditionalSteps") for result in report.results} >= {0, 1, 2}
    assert "load_exhaustion_tck_cases" in graphblocks_testing.__all__


def test_testing_package_exhaustion_tck_rejects_boolean_continuation_permit_epoch(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.exhaustion(
        case_id="exhaustion/boolean-continuation-permit-epoch",
        fixture={
            "kind": "checkpoint_and_pause",
            "policy": {
                "preset": "checkpoint_and_pause",
                "unit": "turn",
                "continuation": {
                    "allowedWork": ["resume"],
                    "maxAdditionalSteps": 1,
                    "deadline": "2026-06-22T01:00:00Z",
                },
            },
            "continuationPermit": {
                "admissionEpoch": True,
                "authorizedUsage": [{"kind": "model_output_tokens", "amount": 1, "unit": "tokens"}],
            },
            "expected": {
                "error": "budget permit admission_epoch must be an integer",
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["error"] == "budget permit admission_epoch must be an integer"


def test_testing_package_exhaustion_controller_rejects_boolean_work_epoch(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    policy = graphblocks_testing.ExhaustionPolicy.from_preset(
        "checkpoint_and_pause",
        unit="turn",
        continuation=graphblocks_testing.ContinuationEnvelope(
            allowed_work={"checkpoint"},
            max_additional_steps=1,
        ),
    )
    controller = graphblocks_testing.ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
    )

    try:
        controller.admit("checkpoint", work_epoch=True)
    except ValueError as error:
        assert str(error) == "exhaustion work_epoch must be an integer"
    else:
        raise AssertionError("boolean work_epoch was accepted")


def test_testing_package_exhaustion_tck_rejects_boolean_admission_permit_epoch(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.exhaustion(
        case_id="exhaustion/boolean-admission-permit-epoch",
        fixture={
            "kind": "checkpoint_and_pause",
            "policy": {
                "preset": "checkpoint_and_pause",
                "unit": "turn",
                "continuation": {
                    "allowedWork": ["checkpoint"],
                    "maxAdditionalSteps": 1,
                    "deadline": "2026-06-22T01:00:00Z",
                },
            },
            "admissionEpoch": 7,
            "admissions": [
                {
                    "workKind": "checkpoint",
                    "workEpoch": 8,
                    "permit": {
                        "admissionEpoch": True,
                        "authorizedUsage": [
                            {"kind": "model_output_tokens", "amount": 1, "unit": "tokens"}
                        ],
                    },
                    "allowed": False,
                    "reason": "invalid_permit",
                }
            ],
            "expected": {
                "error": "budget permit admission_epoch must be an integer",
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["error"] == "budget permit admission_epoch must be an integer"


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


def test_testing_package_budget_race_tck_rejects_boolean_expected_amount(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.budget_race(
        case_id="budget-race/boolean-expected-amount",
        fixture={
            "kind": "reservation_race",
            "budgetId": "budget-1",
            "scope": "tenant:acme",
            "policyRef": "policy-1",
            "allocated": [{"kind": "model_total_tokens", "amount": 100, "unit": "tokens"}],
            "owners": ["run:1"],
            "reservationAmounts": [{"kind": "model_total_tokens", "amount": 70, "unit": "tokens"}],
            "expiresAt": "later",
            "expectedAllowed": 1,
            "expectedDenied": 0,
            "expectedReserved": [{"kind": "model_total_tokens", "amount": True, "unit": "tokens"}],
            "expected": {"error": "usage amount must be a decimal"},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["error"] == "usage amount must be a decimal"


def test_testing_package_loads_shared_conversation_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_conversation_tck_cases(ROOT / "tck" / "conversation" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["conversation"] * 10
    assert report.ok
    assert {case.case_id for case in cases} == {
        "turn_draft_commits_atomically",
        "abort_turn_retracts_draft_without_commit",
        "policy_stop_retracts_draft_without_commit",
        "commit_conflict_marks_turn_failed",
        "branch_and_regenerate_preserve_lineage",
        "branch_respects_attachment_scope_and_include_flag",
        "attachment_resolution_filters_by_readiness_message_and_conversation_scope",
        "archive_marks_conversation_terminal_for_appends",
        "compaction_records_source_output_and_token_delta",
        "delete_retention_distinguishes_tombstone_and_hard_delete",
    }
    assert any(result.observed.get("terminalCommitDenied") is True for result in report.results)
    assert any(result.observed.get("sourceMessageStatuses") == ["committed", "superseded", "committed"] for result in report.results)
    assert any(result.observed.get("branchAttachmentIds") == ["att-1", "att-conversation"] for result in report.results)
    assert any(result.observed.get("withoutConversationScopeIds") == ["att-message"] for result in report.results)
    assert any(result.observed.get("appendRejected") is True for result in report.results)
    assert any(result.observed.get("compactionIds") == ["compact-1"] for result in report.results)
    assert any(result.observed.get("hardDeleted") is True for result in report.results)
    assert "load_conversation_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_documents_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_documents_tck_cases(ROOT / "tck" / "documents" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["documents"] * 6
    assert report.ok
    assert {case.case_id for case in cases} == {
        "plain_text_revision_parse_preserves_lineage",
        "line_chunks_preserve_source_spans_and_acl",
        "parser_selection_lock_is_deterministic_and_records_inputs",
        "parser_selection_ocr_fallback_is_explicit_and_deterministic",
        "parser_locked_parse_rejects_artifact_checksum_mismatch",
        "invalid_chunk_size_is_rejected",
    }
    assert any(result.observed.get("sourceRefDigestMatches") is True for result in report.results)
    assert any(result.observed.get("processorId") == "a-parser" for result in report.results)
    assert any(result.observed.get("reason") == "ocr_fallback" for result in report.results)
    assert any(result.observed.get("error") == "artifact_checksum_mismatch" for result in report.results)
    assert any(result.observed.get("error") == "invalid_max_elements" for result in report.results)
    assert "load_documents_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_deployment_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_deployment_tck_cases(ROOT / "tck" / "deployment" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["deployment"] * 5
    assert report.ok
    assert {case.case_id for case in cases} == {
        "deployment_revision_digest_ignores_record_identity",
        "mutable_production_release_references_are_rejected",
        "workload_aware_upgrade_policy_preserves_drain_semantics",
        "rollout_gate_holds_advances_and_aborts_without_unsafe_rollback",
        "slo_profile_reports_failed_and_missing_conditions",
    }
    assert any(result.observed.get("error") == "mutable_references" for result in report.results)
    assert any(
        {"kind": "drain_on_old", "revisionId": "rev-old", "fromRevisionId": None, "toRevisionId": None}
        in result.observed.get("decisions", [])
        for result in report.results
    )
    assert "load_deployment_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_durable_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    raw_cases = json.loads((ROOT / "tck" / "durable" / "cases.json").read_text(encoding="utf-8"))
    resume_token_hashes = [
        case["operation"]["resumeTokenHash"]
        for case in raw_cases
        if isinstance(case, dict)
        and isinstance(case.get("operation"), dict)
        and "resumeTokenHash" in case["operation"]
    ]

    cases = graphblocks_testing.load_durable_tck_cases(ROOT / "tck" / "durable" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["durable"] * 13
    assert resume_token_hashes
    assert all(
        isinstance(token_hash, str)
        and token_hash.startswith("sha256:")
        and len(token_hash.removeprefix("sha256:")) == 64
        and all(character in "0123456789abcdef" for character in token_hash.removeprefix("sha256:"))
        for token_hash in resume_token_hashes
    )
    assert report.ok
    assert {case.case_id for case in cases} == {
        "source_cursor_replay_and_commit_advances",
        "source_rejects_unknown_cursor_and_stale_commit",
        "window_watermark_closes_after_allowed_lateness",
        "sink_idempotency_replays_and_rejects_conflict",
        "checkpoint_barrier_and_replay_latest_compatible",
        "tool_terminal_record_projects_tool_result",
        "tool_terminal_rejects_expired_committed_effect",
        "policy_stop_denies_late_durable_result_but_records_effect_outcome",
        "background_run_detach_replay_and_cursor_expiry",
        "webhook_delivery_retry_duplicate_and_dead_letter_redrive",
        "async_callback_resume_auth_schema_stale_and_budget_guards",
        "callback_cancel_race_cancel_wins_and_blocks_resume",
        "external_operation_late_side_effect_usage_reconciliation",
    }
    assert any(result.observed.get("replayOffsets") == [11, 12] for result in report.results)
    assert any(result.observed.get("lateDurableResultError") == "response_policy_stopped" for result in report.results)
    assert any(result.observed.get("cancelWinsBlocksResume") is True for result in report.results)
    assert "load_durable_tck_cases" in graphblocks_testing.__all__


def test_testing_package_rejects_failed_callback_delivery_without_error_evidence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/missing-callback-delivery-error",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                }
            ],
            "expected": {"retryScheduledAfter5xx": True},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "failed callback delivery requires lastError",
            "path": "$.deliveries[0].lastError",
        },
    )


def test_testing_package_rejects_callback_delivery_without_idempotency_evidence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/missing-callback-delivery-idempotency",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "expected": {"retryScheduledAfter5xx": True},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery requires idempotencyKey",
            "path": "$.deliveries[0].idempotencyKey",
        },
    )


def test_testing_package_rejects_duplicate_callback_delivery_idempotency_keys(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/duplicate-callback-delivery-idempotency",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                },
                {
                    "deliveryId": "del-002",
                    "subscriptionId": "sub-ide-002",
                    "eventId": "evt-0101",
                    "runId": "run-coding-001",
                    "sequence": 101,
                    "cursor": "evt-0101",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 409,
                    "status": "acknowledged",
                    "deliveredAt": "2026-07-02T00:00:01Z",
                    "acknowledgedAt": "2026-07-02T00:00:02Z",
                },
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery idempotencyKey must be unique",
            "path": "$.deliveries[1].idempotencyKey",
        },
    )


def test_testing_package_rejects_non_object_callback_delivery_evidence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/non-object-callback-delivery",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": ["del-001"],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery must be object",
            "path": "$.deliveries[0]",
        },
    )


def test_testing_package_rejects_empty_callback_delivery_evidence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/empty-callback-deliveries",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery requires at least one delivery",
            "path": "$.deliveries",
        },
    )


def test_testing_package_rejects_non_object_callback_redrive_evidence(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/non-object-callback-redrive",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "redrive": "del-dead-001",
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackRedriveInvalid",
            "message": "callback redrive must be object",
            "path": "$.redrive",
        },
    )


def test_testing_package_rejects_non_boolean_callback_projection_outage_flag(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/non-boolean-callback-projection-outage",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "nonMandatoryOutageBlocksRun": "false",
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackProjectionInvalid",
            "message": "callback projection requires boolean nonMandatoryOutageBlocksRun",
            "path": "$.nonMandatoryOutageBlocksRun",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("deliveryId", " ", "callback delivery requires deliveryId"),
        ("eventId", "", "callback delivery requires eventId"),
        ("runId", None, "callback delivery requires runId"),
        ("sequence", "100", "callback delivery requires integer sequence"),
    ],
)
def test_testing_package_rejects_callback_delivery_without_identity_evidence(
    monkeypatch, field: str, value: object, message: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    delivery = {
        "deliveryId": "del-001",
        "subscriptionId": "sub-ide-001",
        "eventId": "evt-0100",
        "runId": "run-coding-001",
        "sequence": 100,
        "cursor": "evt-0100",
        "attempt": 1,
        "idempotencyKey": "sub-ide-001:evt-0100",
        "receiverStatus": 500,
        "status": "failed",
        "nextRetryAt": "2026-07-02T00:00:10Z",
        "lastError": "receiver_error",
    }
    delivery[field] = value
    case = graphblocks_testing.TckCase.durable(
        case_id=f"durable/missing-callback-delivery-{field}",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [delivery],
            "expected": {"retryScheduledAfter5xx": True},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": message,
            "path": f"$.deliveries[0].{field}",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("subscriptionId", " ", "callback delivery requires subscriptionId"),
        ("cursor", "", "callback delivery requires cursor"),
        ("attempt", "1", "callback delivery requires integer attempt"),
    ],
)
def test_testing_package_rejects_callback_delivery_without_envelope_evidence(
    monkeypatch, field: str, value: object, message: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    delivery = {
        "deliveryId": "del-001",
        "subscriptionId": "sub-ide-001",
        "eventId": "evt-0100",
        "runId": "run-coding-001",
        "sequence": 100,
        "cursor": "evt-0100",
        "attempt": 1,
        "idempotencyKey": "sub-ide-001:evt-0100",
        "receiverStatus": 500,
        "status": "failed",
        "nextRetryAt": "2026-07-02T00:00:10Z",
        "lastError": "receiver_error",
    }
    delivery[field] = value
    case = graphblocks_testing.TckCase.durable(
        case_id=f"durable/missing-callback-delivery-{field}",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [delivery],
            "expected": {"retryScheduledAfter5xx": True},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": message,
            "path": f"$.deliveries[0].{field}",
        },
    )


def test_testing_package_rejects_callback_delivery_with_invalid_status(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/invalid-callback-delivery-status",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 200,
                    "status": "mystery",
                }
            ],
            "nonMandatoryOutageBlocksRun": False,
            "expected": {"nonMandatoryOutageBlocksRun": False},
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery has invalid status",
            "path": "$.deliveries[0].status",
        },
    )


def test_testing_package_rejects_callback_delivery_with_non_integer_receiver_status(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/non-integer-callback-delivery-receiver-status",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": "500",
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery requires integer receiverStatus",
            "path": "$.deliveries[0].receiverStatus",
        },
    )


def test_testing_package_rejects_callback_delivery_with_blank_next_retry_at(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/blank-callback-delivery-next-retry",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "",
                    "lastError": "receiver_error",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery requires nextRetryAt timestamp",
            "path": "$.deliveries[0].nextRetryAt",
        },
    )


def test_testing_package_rejects_callback_delivery_with_invalid_next_retry_at(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/invalid-callback-delivery-next-retry",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "later",
                    "lastError": "receiver_error",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "callback delivery requires nextRetryAt timestamp",
            "path": "$.deliveries[0].nextRetryAt",
        },
    )


def test_testing_package_rejects_delivered_callback_delivery_without_delivered_at(
    monkeypatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/missing-callback-delivery-delivered-at",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 200,
                    "status": "delivered",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "delivered callback delivery requires deliveredAt",
            "path": "$.deliveries[0].deliveredAt",
        },
    )


def test_testing_package_rejects_acknowledged_callback_delivery_without_acknowledged_at(
    monkeypatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/missing-callback-delivery-acknowledged-at",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 409,
                    "status": "acknowledged",
                    "deliveredAt": "2026-07-02T00:00:01Z",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "acknowledged callback delivery requires acknowledgedAt",
            "path": "$.deliveries[0].acknowledgedAt",
        },
    )


def test_testing_package_rejects_acknowledged_callback_delivery_before_delivered_at(
    monkeypatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/callback-delivery-acknowledged-before-delivered",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 409,
                    "status": "acknowledged",
                    "deliveredAt": "2026-07-02T00:00:02Z",
                    "acknowledgedAt": "2026-07-02T00:00:01Z",
                }
            ],
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackDeliveryInvalid",
            "message": "acknowledgedAt must not be before deliveredAt",
            "path": "$.deliveries[0].acknowledgedAt",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operatorPrincipal", " ", "callback redrive requires operatorPrincipal"),
        ("reason", " ", "callback redrive requires reason"),
    ],
)
def test_testing_package_rejects_callback_redrive_without_audit_evidence(
    monkeypatch, field: str, value: object, message: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    redrive = {
        "deliveryId": "del-dead-001",
        "eventId": "evt-0100",
        "originalEventId": "evt-0100",
        "operatorPrincipal": "operator-1",
        "reason": "operator redrive",
        "createsApplicationEvent": False,
    }
    redrive[field] = value
    case = graphblocks_testing.TckCase.durable(
        case_id=f"durable/missing-callback-redrive-{field}",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "redrive": redrive,
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackRedriveInvalid",
            "message": message,
            "path": f"$.redrive.{field}",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("deliveryId", "", "callback redrive requires deliveryId"),
        ("eventId", " ", "callback redrive requires eventId"),
        ("originalEventId", None, "callback redrive requires originalEventId"),
    ],
)
def test_testing_package_rejects_callback_redrive_without_identity_evidence(
    monkeypatch, field: str, value: object, message: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    redrive = {
        "deliveryId": "del-dead-001",
        "eventId": "evt-0100",
        "originalEventId": "evt-0100",
        "operatorPrincipal": "operator-1",
        "reason": "operator redrive",
        "createsApplicationEvent": False,
    }
    redrive[field] = value
    case = graphblocks_testing.TckCase.durable(
        case_id=f"durable/missing-callback-redrive-{field}",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "redrive": redrive,
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackRedriveInvalid",
            "message": message,
            "path": f"$.redrive.{field}",
        },
    )


def test_testing_package_rejects_callback_redrive_that_changes_event_identity(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/callback-redrive-event-identity-mismatch",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "redrive": {
                "deliveryId": "del-dead-001",
                "eventId": "evt-forged",
                "originalEventId": "evt-0100",
                "operatorPrincipal": "operator-1",
                "reason": "operator redrive",
                "createsApplicationEvent": False,
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackRedriveInvalid",
            "message": "callback redrive must preserve originalEventId",
            "path": "$.redrive.eventId",
        },
    )


def test_testing_package_rejects_callback_redrive_with_non_boolean_application_event_flag(
    monkeypatch,
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    case = graphblocks_testing.TckCase.durable(
        case_id="durable/callback-redrive-application-event-flag",
        fixture={
            "kind": "callback_delivery_projection",
            "deliveries": [
                {
                    "deliveryId": "del-001",
                    "subscriptionId": "sub-ide-001",
                    "eventId": "evt-0100",
                    "runId": "run-coding-001",
                    "sequence": 100,
                    "cursor": "evt-0100",
                    "attempt": 1,
                    "idempotencyKey": "sub-ide-001:evt-0100",
                    "receiverStatus": 500,
                    "status": "failed",
                    "nextRetryAt": "2026-07-02T00:00:10Z",
                    "lastError": "receiver_error",
                }
            ],
            "redrive": {
                "deliveryId": "del-dead-001",
                "eventId": "evt-0100",
                "originalEventId": "evt-0100",
                "operatorPrincipal": "operator-1",
                "reason": "operator redrive",
                "createsApplicationEvent": "false",
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert not report.ok
    assert report.results[0].diagnostics == (
        {
            "code": "DurableCallbackRedriveInvalid",
            "message": "callback redrive requires boolean createsApplicationEvent",
            "path": "$.redrive.createsApplicationEvent",
        },
    )


def test_testing_package_loads_shared_orchestration_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_orchestration_tck_cases(
        ROOT / "tck" / "orchestration" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["orchestration"] * 6
    assert report.ok
    assert {case.case_id for case in cases} == {
        "task_plan_patch_revises_steps_and_preserves_noop_digest",
        "task_plan_dependency_and_cycle_errors_are_explicit",
        "context_access_digest_is_order_stable_and_rejects_unknown_resource",
        "model_pool_selects_eligible_model_and_rejects_disallowed_tool",
        "lease_pool_enforces_capacity_expiry_fencing_and_release",
        "child_budget_delegation_creates_scoped_permit",
    }
    assert any(result.observed.get("selectedModel") == "support-internal" for result in report.results)
    assert any(result.observed.get("firstLeaseEpoch") == 1 for result in report.results)
    assert "load_orchestration_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_rag_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_rag_tck_cases(ROOT / "tck" / "rag" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["rag"] * 4
    assert report.ok
    assert {case.case_id for case in cases} == {
        "freshness_filter_compares_source_modified_at_as_datetime",
        "grounded_answer_accepts_current_context_source",
        "ungrounded_answer_abstains_when_context_empty",
        "unsupported_claim_abstains_with_validation_failure",
    }
    grounding_results = [
        result for result in report.results if "issueCodes" in result.observed
    ]
    assert {tuple(result.observed["issueCodes"]) for result in grounding_results} == {
        (),
        ("grounding.insufficient_context",),
        ("claim.unsupported_by_citation",),
    }
    freshness = next(
        result
        for result in report.results
        if result.case_id == "freshness_filter_compares_source_modified_at_as_datetime"
    )
    assert freshness.observed["selectedHitIds"] == ["hit-fresh"]
    assert freshness.observed["droppedHitIds"] == ["hit-stale"]
    assert freshness.observed["freshnessSatisfaction"] == "0.5"
    assert any(result.observed.get("abstentionReason") == "insufficient_context" for result in report.results)
    assert "load_rag_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_retry_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_retry_tck_cases(ROOT / "tck" / "retry" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["retry"] * 4
    assert report.ok
    assert {case.case_id for case in cases} == {
        "effect_retry_preserves_idempotency_key",
        "effect_retry_exhaustion_preserves_idempotency_key",
        "filesystem_write_retry_preserves_idempotency_key",
        "cancelled_effect_attempt_does_not_retry",
    }
    assert {tuple(result.observed["retryIdempotencyKeys"]) for result in report.results} == {
        ("ticket-create:request-1", "ticket-create:request-1"),
        ("ticket-create:request-2",),
        ("file-write:request-1", "file-write:request-1"),
        (),
    }
    assert {tuple(result.observed["contextIdempotencyKeys"]) for result in report.results} == {
        ("ticket-create:request-1", "ticket-create:request-1", "ticket-create:request-1"),
        ("ticket-create:request-2", "ticket-create:request-2"),
        ("file-write:request-1", "file-write:request-1", "file-write:request-1"),
        ("ticket-create:request-3",),
    }
    assert any(result.observed["status"] == "cancelled" for result in report.results)
    assert "load_retry_tck_cases" in graphblocks_testing.__all__


def test_testing_package_retry_tck_ignores_boolean_cancel_attempt(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    case = graphblocks_testing.TckCase.retry(
        case_id="retry/boolean-cancel-attempt",
        fixture={
            "kind": "node_retry",
            "maxAttempts": 2,
            "failuresBeforeSuccess": 0,
            "cancelOnAttempt": True,
            "idempotencyKey": "ticket-create:boolean-cancel",
            "expected": {
                "status": "succeeded",
                "terminalKind": "run_succeeded",
                "attempts": 1,
                "retryCount": 0,
                "contextIdempotencyKeys": ["ticket-create:boolean-cancel"],
            },
        },
    )

    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases((case,))

    assert report.ok
    assert report.results[0].observed["status"] == "succeeded"
    assert "node_cancelled" not in report.results[0].observed["journalKinds"]


def test_testing_package_loads_shared_tool_lifecycle_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_tool_lifecycle_tck_cases(
        ROOT / "tck" / "tool-lifecycle" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["tool-lifecycle"] * 18
    assert report.ok
    assert {case.case_id for case in cases} == {
        "incremental_arguments_do_not_finalize_call",
        "invalid_arguments_denied_before_policy_admission",
        "missing_schema_denies_tool_admission",
        "resolved_tool_mismatch_denies_tool_admission",
        "tool_name_mismatch_denies_tool_admission",
        "arguments_digest_mismatch_denies_tool_admission",
        "policy_stopped_response_denies_tool_admission",
        "expired_policy_decision_denies_tool_admission",
        "expired_resolved_tool_denies_tool_admission",
        "policy_input_digest_mismatch_denies_tool_admission",
        "missing_policy_input_digest_denies_tool_admission",
        "policy_denied_decision_denies_tool_admission",
        "policy_deferred_decision_denies_tool_admission",
        "missing_approval_denies_tool_admission",
        "expired_approval_denies_tool_admission",
        "required_idempotency_key_missing_denies_tool_admission",
        "blank_idempotency_key_denies_tool_admission",
        "approval_invalid_after_argument_mutation",
    }
    assert any(not result.observed.get("finalizedBeforeComplete", True) for result in report.results)
    assert any(result.observed.get("schemaRejectedBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("schemaMissingBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("resolvedToolMismatchBeforeSchema") is True for result in report.results)
    assert any(result.observed.get("toolNameMismatchBeforeSchema") is True for result in report.results)
    assert any(
        result.observed.get("argumentsDigestRejectedBeforeSchema") is True for result in report.results
    )
    assert any(result.observed.get("policyStoppedBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("policyExpiredBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("resolvedToolExpiredBeforeApproval") is True for result in report.results)
    assert any(
        result.observed.get("policyDigestRejectedBeforeApproval") is True for result in report.results
    )
    assert any(
        result.observed.get("policyDigestMissingBeforeApproval") is True for result in report.results
    )
    assert any(result.observed.get("policyDeniedBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("policyDeferredBeforeApproval") is True for result in report.results)
    assert any(result.observed.get("approvalRequiredBeforeIdempotency") is True for result in report.results)
    assert any(
        result.observed.get("expiredApprovalRejectedBeforeIdempotency") is True for result in report.results
    )
    assert any(result.observed.get("idempotencyRejectedAfterApproval") is True for result in report.results)
    assert any(
        result.observed.get("blankIdempotencyRejectedAfterApproval") is True for result in report.results
    )
    assert any(result.observed.get("mutatedApprovalValid") is False for result in report.results)
    assert "load_tool_lifecycle_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_tool_execution_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_tool_execution_tck_cases(
        ROOT / "tck" / "tool-execution" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["tool-execution"] * 19
    assert report.ok
    assert {case.case_id for case in cases} == {
        "independent_read_tools_execute_concurrently",
        "conflicting_write_tools_with_same_effect_key_are_rejected",
        "dependency_serialized_write_tools_share_effect_key",
        "duplicate_dependencies_are_rejected",
        "parallel_state_changing_tools_require_effect_keys",
        "dependency_serialized_write_tools_do_not_require_effect_keys",
        "parallel_filesystem_write_tools_require_effect_keys",
        "parallel_process_and_destructive_tools_require_effect_keys",
        "policy_abort_denies_pending_tool_calls",
        "policy_abort_cancels_running_read_and_denies_pending",
        "policy_abort_preserves_running_state_changing_call_without_safe_cancellation",
        "failed_dependency_skips_dependents",
        "policy_stopped_dependency_skips_dependents",
        "denied_dependency_skips_dependent_and_allows_independent",
        "expired_dependency_skips_dependent",
        "cancel_dependents_policy_cancels_dependents",
        "allow_independent_cancellation_skips_dependents",
        "cancel_all_policy_cancels_nonterminal_calls",
        "fail_fast_policy_cancels_pending_calls_after_failure",
    }
    assert any(result.observed.get("creationError") == "unsafe_parallel_effects" for result in report.results)
    assert any(result.observed.get("creationError") == "duplicate_dependency" for result in report.results)
    assert any(
        result.observed.get("states") == {"call-a": "running", "call-b": "running"}
        for result in report.results
    )
    assert any(
        result.observed.get("states") == {"call-a": "running", "call-b": "denied"}
        for result in report.results
    )
    assert any(
        result.observed.get("states") == {"call-a": "cancelled", "call-b": "denied"}
        for result in report.results
    )
    assert "load_tool_execution_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_tool_result_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_tool_result_tck_cases(
        ROOT / "tck" / "tool-result" / "cases.json"
    )
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["tool-result"] * 6
    assert report.ok
    assert {case.case_id for case in cases} == {
        "completed_tool_result_is_labeled_redacted_and_captured",
        "invalid_json_output_schema_is_rejected_before_model_return",
        "stale_output_digest_is_rejected_before_model_return",
        "artifact_reference_mode_rejects_inline_output",
        "stream_state_requires_started_before_incremental_output",
        "stream_state_rejects_denied_result_with_committed_effect",
    }
    assert any(result.observed.get("texts") == ["safe [redacted] suffix"] for result in report.results)
    assert any("expected string" in str(result.observed.get("error")) for result in report.results)
    assert any("output digest does not match" in str(result.observed.get("error")) for result in report.results)
    assert any("artifact_reference mode" in str(result.observed.get("error")) for result in report.results)
    assert any(
        result.observed.get("errors")
        == [
            {"operation": 0, "code": "EventBeforeStarted"},
            {"operation": 1, "code": "EventBeforeStarted"},
            {"operation": 4, "code": "DuplicateStarted"},
        ]
        for result in report.results
    )
    assert any(
        result.observed.get("errors") == [{"operation": 0, "code": "InvalidEvent"}]
        and result.observed.get("finalStatuses") == {"call-2": "denied"}
        for result in report.results
    )
    assert "load_tool_result_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_usage_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_usage_tck_cases(ROOT / "tck" / "usage" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["usage"] * 3
    assert report.ok
    assert {tuple(result.observed["recordIds"]) for result in report.results} == {
        ("usage-provisional", "usage-reconciled"),
        ("usage-provider-1",),
        (),
    }
    assert {tuple(result.observed["appendResults"]) for result in report.results} == {
        ("usage-provisional", "usage-reconciled"),
        ("usage-provider-1", "usage-provider-1"),
        (),
    }
    assert any(
        result.observed.get("errors") == [
            {"operation": 0, "message": "usage amount must be non-negative"}
        ]
        for result in report.results
    )
    assert "load_usage_tck_cases" in graphblocks_testing.__all__


def test_testing_package_loads_shared_voice_tck_cases(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-voice" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    cases = graphblocks_testing.load_voice_tck_cases(ROOT / "tck" / "voice" / "cases.json")
    report = graphblocks_testing.TckRunner(graphblocks_testing.stdlib_registry()).run_cases(cases)

    assert [case.kind for case in cases] == ["voice"] * 4
    assert report.ok
    assert {case.case_id for case in cases} == {
        "duplex_session_request_contract_tracks_turn_and_tools",
        "vad_authority_drives_interruption_classifier",
        "playback_ledger_interrupts_active_items_only",
        "voice_contract_validation_errors_are_explicit",
    }
    assert any(result.observed.get("interruptionKind") == "interrupt" for result in report.results)
    assert any(result.observed.get("transportError") == "voice_contract_error" for result in report.results)
    assert "load_voice_tck_cases" in graphblocks_testing.__all__


def test_testing_package_discovers_all_shared_tck_suite_manifests(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    manifests = graphblocks_testing.load_tck_suite_manifests(ROOT / "tck")
    by_suite = {manifest.suite_id: manifest for manifest in manifests}

    assert tuple(by_suite) == (
        "application-events",
        "application-protocol",
        "approval-review",
        "budget-race",
        "compiler",
        "conversation",
        "deployment",
        "documents",
        "durable",
        "exhaustion",
        "orchestration",
        "policy",
        "rag",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
        "usage",
        "voice",
    )
    assert by_suite["budget-race"].case_ids == (
        "competing_reservations_serialize_against_available_budget",
        "completion_reserve_allows_only_one_concurrent_spender",
    )
    assert by_suite["schema"].auxiliary_paths == ("schema/typed-values.json",)
    assert by_suite["schema"].manifest_contract()["auxiliary_paths"] == ["schema/typed-values.json"]
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
        "tool_call_lifecycle_states_emit_standard_events",
        "output_cutoff_discards_late_commit_for_same_response",
        "output_cutoff_marks_draft_incomplete",
        "output_policy_events_track_evaluation_and_decisions",
        "tool_result_delta_is_draft_until_completed",
        "terminal_tool_result_events_preserve_partial_status",
        "failed_and_denied_tool_result_events_are_terminal",
        "stream_rejects_conflicting_duplicate_ids_and_nonmonotonic_sequences",
    )
    assert by_suite["application-protocol"].case_ids == (
        "application_protocol_kind_sets_match_contract",
        "command_envelope_preserves_metadata_and_payload",
        "event_envelope_accepts_output_cutoff_event",
        "event_envelope_preserves_async_operation_metadata",
        "command_envelope_rejects_non_object_payload",
        "event_envelope_rejects_non_object_payload",
        "capability_negotiation_intersects_commands_and_events",
        "capability_negotiation_rejects_blank_protocol_version",
        "protocol_log_suppresses_duplicates_and_replays_after_cursor",
        "protocol_log_rejects_events_from_another_run",
        "protocol_log_rejects_mutated_duplicate_event_ids",
        "protocol_stream_cutoff_discards_late_output",
    )
    assert by_suite["approval-review"].case_ids == (
        "review_request_digest_is_scope_order_invariant",
        "credentialed_review_completes_required_scope",
        "changed_review_subject_is_rejected",
        "invalidated_review_does_not_complete_scope",
        "missing_reviewer_credential_is_rejected",
        "expired_reviewer_credential_is_rejected",
    )
    assert by_suite["conversation"].case_ids == (
        "turn_draft_commits_atomically",
        "abort_turn_retracts_draft_without_commit",
        "policy_stop_retracts_draft_without_commit",
        "commit_conflict_marks_turn_failed",
        "branch_and_regenerate_preserve_lineage",
        "branch_respects_attachment_scope_and_include_flag",
        "compaction_records_source_output_and_token_delta",
        "delete_retention_distinguishes_tombstone_and_hard_delete",
        "attachment_resolution_filters_by_readiness_message_and_conversation_scope",
        "archive_marks_conversation_terminal_for_appends",
    )
    assert by_suite["deployment"].case_ids == (
        "deployment_revision_digest_ignores_record_identity",
        "mutable_production_release_references_are_rejected",
        "workload_aware_upgrade_policy_preserves_drain_semantics",
        "rollout_gate_holds_advances_and_aborts_without_unsafe_rollback",
        "slo_profile_reports_failed_and_missing_conditions",
    )
    assert by_suite["documents"].case_ids == (
        "plain_text_revision_parse_preserves_lineage",
        "line_chunks_preserve_source_spans_and_acl",
        "parser_selection_lock_is_deterministic_and_records_inputs",
        "parser_selection_ocr_fallback_is_explicit_and_deterministic",
        "parser_locked_parse_rejects_artifact_checksum_mismatch",
        "invalid_chunk_size_is_rejected",
    )
    assert by_suite["durable"].case_ids == (
        "source_cursor_replay_and_commit_advances",
        "source_rejects_unknown_cursor_and_stale_commit",
        "window_watermark_closes_after_allowed_lateness",
        "sink_idempotency_replays_and_rejects_conflict",
        "checkpoint_barrier_and_replay_latest_compatible",
        "tool_terminal_record_projects_tool_result",
        "tool_terminal_rejects_expired_committed_effect",
        "policy_stop_denies_late_durable_result_but_records_effect_outcome",
        "background_run_detach_replay_and_cursor_expiry",
        "webhook_delivery_retry_duplicate_and_dead_letter_redrive",
        "async_callback_resume_auth_schema_stale_and_budget_guards",
        "callback_cancel_race_cancel_wins_and_blocks_resume",
        "external_operation_late_side_effect_usage_reconciliation",
    )
    assert by_suite["orchestration"].case_ids == (
        "task_plan_patch_revises_steps_and_preserves_noop_digest",
        "task_plan_dependency_and_cycle_errors_are_explicit",
        "context_access_digest_is_order_stable_and_rejects_unknown_resource",
        "model_pool_selects_eligible_model_and_rejects_disallowed_tool",
        "lease_pool_enforces_capacity_expiry_fencing_and_release",
        "child_budget_delegation_creates_scoped_permit",
    )
    assert by_suite["tool-lifecycle"].case_ids == (
        "incremental_arguments_do_not_finalize_call",
        "invalid_arguments_denied_before_policy_admission",
        "missing_schema_denies_tool_admission",
        "resolved_tool_mismatch_denies_tool_admission",
        "tool_name_mismatch_denies_tool_admission",
        "arguments_digest_mismatch_denies_tool_admission",
        "policy_stopped_response_denies_tool_admission",
        "expired_policy_decision_denies_tool_admission",
        "expired_resolved_tool_denies_tool_admission",
        "policy_input_digest_mismatch_denies_tool_admission",
        "missing_policy_input_digest_denies_tool_admission",
        "policy_denied_decision_denies_tool_admission",
        "policy_deferred_decision_denies_tool_admission",
        "missing_approval_denies_tool_admission",
        "expired_approval_denies_tool_admission",
        "required_idempotency_key_missing_denies_tool_admission",
        "blank_idempotency_key_denies_tool_admission",
        "approval_invalid_after_argument_mutation",
    )
    assert by_suite["tool-result"].case_ids == (
        "completed_tool_result_is_labeled_redacted_and_captured",
        "invalid_json_output_schema_is_rejected_before_model_return",
        "stale_output_digest_is_rejected_before_model_return",
        "artifact_reference_mode_rejects_inline_output",
        "stream_state_requires_started_before_incremental_output",
        "stream_state_rejects_denied_result_with_committed_effect",
    )
    assert by_suite["retry"].case_ids == (
        "effect_retry_preserves_idempotency_key",
        "effect_retry_exhaustion_preserves_idempotency_key",
        "filesystem_write_retry_preserves_idempotency_key",
        "cancelled_effect_attempt_does_not_retry",
    )
    assert by_suite["rag"].case_ids == (
        "grounded_answer_accepts_current_context_source",
        "ungrounded_answer_abstains_when_context_empty",
        "unsupported_claim_abstains_with_validation_failure",
        "freshness_filter_compares_source_modified_at_as_datetime",
    )
    assert by_suite["tool-execution"].case_ids == (
        "independent_read_tools_execute_concurrently",
        "conflicting_write_tools_with_same_effect_key_are_rejected",
        "dependency_serialized_write_tools_share_effect_key",
        "duplicate_dependencies_are_rejected",
        "parallel_state_changing_tools_require_effect_keys",
        "dependency_serialized_write_tools_do_not_require_effect_keys",
        "parallel_filesystem_write_tools_require_effect_keys",
        "parallel_process_and_destructive_tools_require_effect_keys",
        "policy_abort_denies_pending_tool_calls",
        "policy_abort_cancels_running_read_and_denies_pending",
        "policy_abort_preserves_running_state_changing_call_without_safe_cancellation",
        "failed_dependency_skips_dependents",
        "policy_stopped_dependency_skips_dependents",
        "denied_dependency_skips_dependent_and_allows_independent",
        "expired_dependency_skips_dependent",
        "cancel_dependents_policy_cancels_dependents",
        "allow_independent_cancellation_skips_dependents",
        "cancel_all_policy_cancels_nonterminal_calls",
        "fail_fast_policy_cancels_pending_calls_after_failure",
    )
    assert by_suite["voice"].case_ids == (
        "duplex_session_request_contract_tracks_turn_and_tools",
        "vad_authority_drives_interruption_classifier",
        "playback_ledger_interrupts_active_items_only",
        "voice_contract_validation_errors_are_explicit",
    )
    assert by_suite["budget-race"].content_digest().startswith("sha256:")
    assert "TckSuiteManifest" in graphblocks_testing.__all__
    assert "load_tck_suite_manifests" in graphblocks_testing.__all__


def test_testing_package_cli_lists_tck_suite_manifests(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    assert graphblocks_testing.main(["list", str(ROOT / "tck"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["suiteCount"] == 22
    assert payload["suites"][0]["suite_id"] == "application-events"
    assert payload["suites"][0]["case_count"] == 8
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
        "approval-review",
        "budget-race",
        "compiler",
        "exhaustion",
        "policy",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
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


def test_testing_package_cli_runs_runtime_tck_native_profile_with_fallback_metadata(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "runtime", str(ROOT / "tck" / "runtime" / "cases.json"), "--profile", "native", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["profile"] == "native"
    assert {result["kind"] for result in payload["results"]} == {"runtime"}
    observed = {result["case_id"]: result["observed"] for result in payload["results"]}
    for case_id in (
        "prompt_render_output",
        "control_map_renders_each_item",
        "control_select_treats_null_as_present",
        "tools_resolve_feeds_scripted_agent",
    ):
        assert observed[case_id]["runtime"] in {"native", "local"}
        assert observed[case_id]["runtime"] == "native" or observed[case_id][
            "native_fallback_reason"
        ] == "native_runtime_unavailable"
    assert observed["policy_stopped_turn_rejects_commit"]["native_fallback_reason"] == "missing_native_node_outputs"
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_native_runtime_tck_writes_evidence_paths(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    calls: list[dict[str, object]] = []

    def run_test_graph(
        graph: dict[str, object],
        _inputs: dict[str, object],
        node_outputs: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        calls.append(options)
        graph_name = graph["metadata"]["name"]  # type: ignore[index]
        if graph_name == "runtime-prompt-render":
            outputs = {"prompt": node_outputs["render"]["prompt"]}  # type: ignore[index]
        elif graph_name == "runtime-control-map":
            outputs = {"values": node_outputs["map"]["values"]}  # type: ignore[index]
        elif graph_name == "runtime-control-select":
            outputs = {
                "selected": node_outputs["select"]["selected"],  # type: ignore[index]
                "value": node_outputs["select"]["value"],  # type: ignore[index]
            }
        else:
            outputs = {"candidate": node_outputs["agent"]["candidate"]}  # type: ignore[index]
        return {
            "runId": options["run_id"],
            "status": "succeeded",
            "outputs": outputs,
            "journal": [
                {"kind": "run_started", "runId": options["run_id"]},
                {"kind": "run_succeeded", "runId": options["run_id"], "terminal": True},
            ],
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_test_graph=run_test_graph),
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    evidence_dir = tmp_path / "native-evidence"

    exit_code = graphblocks_testing.main(
        [
            "run",
            "runtime",
            str(ROOT / "tck" / "runtime" / "cases.json"),
            "--profile",
            "native",
            "--evidence-dir",
            str(evidence_dir),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    observed = {result["case_id"]: result["observed"] for result in payload["results"]}
    prompt_observed = observed["prompt_render_output"]
    assert prompt_observed["runtime"] == "native"
    assert prompt_observed["run_store_path"] == str(evidence_dir / "tck-prompt-render-output-runs.sqlite3")
    assert prompt_observed["journal_store_path"] == str(evidence_dir / "tck-prompt-render-output-journal.sqlite3")
    assert calls[0] == {
        "journal_store_path": str(evidence_dir / "tck-prompt-render-output-journal.sqlite3"),
        "run_id": "tck-prompt-render-output",
        "run_store_path": str(evidence_dir / "tck-prompt-render-output-runs.sqlite3"),
    }
    assert payload["native_evidence"] == {
        "fallback_case_count": 3,
        "fallback_reasons": {"missing_native_node_outputs": 3},
        "journal_store_paths": [
            str(evidence_dir / "tck-control-map-renders-each-item-journal.sqlite3"),
            str(evidence_dir / "tck-control-select-treats-null-as-present-journal.sqlite3"),
            str(evidence_dir / "tck-prompt-render-output-journal.sqlite3"),
            str(evidence_dir / "tck-tools-resolve-feeds-scripted-agent-journal.sqlite3"),
        ],
        "native_case_count": 4,
        "run_store_paths": [
            str(evidence_dir / "tck-control-map-renders-each-item-runs.sqlite3"),
            str(evidence_dir / "tck-control-select-treats-null-as-present-runs.sqlite3"),
            str(evidence_dir / "tck-prompt-render-output-runs.sqlite3"),
            str(evidence_dir / "tck-tools-resolve-feeds-scripted-agent-runs.sqlite3"),
        ],
    }


def test_testing_package_cli_run_all_namespaces_native_tck_evidence(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    calls: list[dict[str, object]] = []

    def run_test_graph(
        graph: dict[str, object],
        _inputs: dict[str, object],
        node_outputs: dict[str, object],
        **options: object,
    ) -> dict[str, object]:
        calls.append(options)
        graph_name = graph["metadata"]["name"]  # type: ignore[index]
        if graph_name == "runtime-prompt-render":
            outputs = {"prompt": node_outputs["render"]["prompt"]}  # type: ignore[index]
        elif graph_name == "runtime-control-map":
            outputs = {"values": node_outputs["map"]["values"]}  # type: ignore[index]
        elif graph_name == "runtime-control-select":
            outputs = {
                "selected": node_outputs["select"]["selected"],  # type: ignore[index]
                "value": node_outputs["select"]["value"],  # type: ignore[index]
            }
        else:
            outputs = {"candidate": node_outputs["agent"]["candidate"]}  # type: ignore[index]
        return {
            "runId": options["run_id"],
            "status": "succeeded",
            "outputs": outputs,
            "journal": [{"kind": "run_succeeded", "runId": options["run_id"], "terminal": True}],
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(run_test_graph=run_test_graph),
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    evidence_dir = tmp_path / "native-evidence"
    tck_root = tmp_path / "tck"
    runtime_dir = tck_root / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "cases.json").write_text((ROOT / "tck" / "runtime" / "cases.json").read_text(encoding="utf-8"), encoding="utf-8")

    exit_code = graphblocks_testing.main(
        ["run-all", str(tck_root), "--profile", "native", "--evidence-dir", str(evidence_dir), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    runtime_report = payload["reports"]["runtime"]
    prompt_observed = {
        result["case_id"]: result["observed"] for result in runtime_report["results"]
    }["prompt_render_output"]
    assert prompt_observed["run_store_path"] == str(
        evidence_dir / "runtime" / "tck-prompt-render-output-runs.sqlite3"
    )
    assert prompt_observed["journal_store_path"] == str(
        evidence_dir / "runtime" / "tck-prompt-render-output-journal.sqlite3"
    )
    assert calls[0]["run_store_path"] == str(evidence_dir / "runtime" / "tck-prompt-render-output-runs.sqlite3")


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


def test_testing_package_cli_runs_application_protocol_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        [
            "run",
            "application-protocol",
            str(ROOT / "tck" / "application-protocol" / "cases.json"),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"application-protocol"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_approval_review_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "approval-review", str(ROOT / "tck" / "approval-review" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"approval-review"}
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


def test_testing_package_cli_runs_retry_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "retry", str(ROOT / "tck" / "retry" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"retry"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_rag_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "rag", str(ROOT / "tck" / "rag" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"rag"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_conversation_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "conversation", str(ROOT / "tck" / "conversation" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"conversation"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_documents_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "documents", str(ROOT / "tck" / "documents" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"documents"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_deployment_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "deployment", str(ROOT / "tck" / "deployment" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"deployment"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_durable_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "durable", str(ROOT / "tck" / "durable" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"durable"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_orchestration_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "orchestration", str(ROOT / "tck" / "orchestration" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"orchestration"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_tool_execution_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "tool-execution", str(ROOT / "tck" / "tool-execution" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"tool-execution"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_tool_result_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "tool-result", str(ROOT / "tck" / "tool-result" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"tool-result"}
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


def test_testing_package_cli_runs_voice_tck_suite(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-voice" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(
        ["run", "voice", str(ROOT / "tck" / "voice" / "cases.json"), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {result["kind"] for result in payload["results"]} == {"voice"}
    assert payload["contentDigest"].startswith("sha256:")


def test_testing_package_cli_runs_all_supported_tck_suites(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-durable" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-voice" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    graphblocks_testing = importlib.import_module("graphblocks_testing")

    exit_code = graphblocks_testing.main(["run-all", str(ROOT / "tck"), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert tuple(payload["reports"]) == (
        "application-events",
        "application-protocol",
        "approval-review",
        "budget-race",
        "compiler",
        "conversation",
        "deployment",
        "documents",
        "durable",
        "exhaustion",
        "orchestration",
        "policy",
        "rag",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
        "usage",
        "voice",
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
    tool_ref = graphblocks_testing.ModelVisibleToolRef(
        tool_name="knowledge.search",
        resolved_tool_id="resolved-search",
        definition_digest="sha256:definition",
        binding_digest="sha256:binding",
        effective_policy_snapshot_id="policy-snapshot-1",
        allowed_for_principal=True,
        valid_until="2026-06-30T00:00:00Z",
    )
    record = store.create_run(
        "sha256:test",
        {},
        deployment_provenance=provenance,
        model_visible_tools=(tool_ref,),
    )
    store.set_status(record.run_id, "succeeded")

    try:
        store.patch_state(record.run_id, {"late": True}, expected_revision=0)
    except graphblocks_testing.RunTerminalStateError as error:
        assert error.status == "succeeded"
        assert "RunDeploymentProvenance" in graphblocks_testing.__all__
        assert "ModelVisibleToolRef" in graphblocks_testing.__all__
        assert record.model_visible_tools == (tool_ref,)
        assert hasattr(graphblocks_testing, "ToolResultStreamState")
        assert hasattr(graphblocks_testing, "ToolResultStreamError")
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
