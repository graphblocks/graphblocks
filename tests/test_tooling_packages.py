from __future__ import annotations

import importlib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_tui_package_builds_run_status_screen_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    screen = graphblocks_tui.run_status_screen(
        run_id="run-1",
        state="running",
        last_event="ToolCallStarted",
        counters={"events": 12, "tool_calls": 2},
    )
    snapshot = graphblocks_tui.TuiSessionSnapshot((screen,))

    assert screen.screen_contract() == {
        "name": "run-status",
        "title": "Run run-1",
        "sections": [
            {"title": "State", "rows": {"last_event": "ToolCallStarted", "state": "running"}},
            {"title": "Counters", "rows": {"events": "12", "tool_calls": "2"}},
        ],
        "commands": [
            {"label": "Refresh", "action": "refresh", "key": "r"},
            {"label": "Cancel", "action": "cancel", "key": "c"},
        ],
    }
    assert snapshot.content_digest().startswith("sha256:")


def test_tui_package_projects_application_protocol_events_to_workspace_screen(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    def event(kind: str, sequence: int, payload: dict[str, object] | None = None):
        event_payload = dict(payload or {})
        if kind == "ToolCallApprovalRequested":
            event_payload["tool_call_id"] = "tool-call-1"
        return graphblocks_client.ApplicationProtocolEvent.new(
            kind,
            graphblocks_client.ApplicationProtocolEventMetadata(
                event_id=f"event-{sequence}",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                sequence=sequence,
                occurred_at_unix_ms=sequence * 1000,
            ),
            payload=payload or {},
        )

    state = graphblocks_tui.TuiProtocolSession("run-1").apply_all(
        (
            event("RunStarted", 1, {"status": "running"}),
            event("AssistantDraftStarted", 2),
            event("AssistantDraftDelta", 3, {"delta": "Hello"}),
            event("AssistantDraftDelta", 4, {"text": " world"}),
            event("AssistantDraftDelta", 4, {"delta": " ignored"}),
            event("ApprovalRequested", 5, {"approval_id": "approval-1"}),
            event("ArtifactReady", 6, {"artifact": {"artifact_id": "artifact-1", "uri": "file:///tmp/out.txt"}}),
            event("AssistantRetracted", 7, {"reason": "policy_denied"}),
        )
    )
    screen = graphblocks_tui.workspace_assistant_screen(state)

    assert state.last_sequence == 7
    assert state.assistant_text == "Hello world"
    assert state.assistant_state == "retracted"
    assert state.pending_actions == ("approval-1",)
    assert state.artifacts == ("artifact-1",)
    assert state.counters["AssistantDraftDelta"] == 2
    assert screen.screen_contract() == {
        "name": "workspace-assistant",
        "title": "Workspace run-1",
        "sections": [
            {"title": "Run", "rows": {"last_event": "AssistantRetracted", "sequence": "7", "status": "running"}},
            {"title": "Assistant", "rows": {"state": "retracted", "text": "Hello world"}},
            {"title": "Pending", "rows": {"actions": "approval-1", "artifacts": "artifact-1"}},
            {
                "title": "Counters",
                "rows": {
                    "ApprovalRequested": "1",
                    "ArtifactReady": "1",
                    "AssistantDraftDelta": "2",
                    "AssistantDraftStarted": "1",
                    "AssistantRetracted": "1",
                    "RunStarted": "1",
                },
            },
        ],
        "commands": [
            {"label": "Refresh", "action": "refresh", "key": "r"},
            {"label": "Cancel", "action": "cancel", "key": "c"},
            {"label": "Approve", "action": "approve", "key": "a"},
            {"label": "Deny", "action": "deny", "key": "d"},
        ],
    }

    mismatched = graphblocks_client.ApplicationProtocolEvent.new(
        "RunStarted",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-other",
            protocol_version="graphblocks.app.v1",
            run_id="run-other",
            sequence=8,
            occurred_at_unix_ms=8000,
        ),
    )
    with pytest.raises(graphblocks_tui.TuiContractError, match="event run_id mismatch"):
        state.apply(mismatched)


def test_tui_package_projects_standard_tool_approval_and_incomplete_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    def event(kind: str, sequence: int, payload: dict[str, object] | None = None):
        event_payload = dict(payload or {})
        if kind == "ToolCallApprovalRequested":
            event_payload["tool_call_id"] = "tool-call-1"
        return graphblocks_client.ApplicationProtocolEvent.new(
            kind,
            graphblocks_client.ApplicationProtocolEventMetadata(
                event_id=f"event-{sequence}",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                sequence=sequence,
                occurred_at_unix_ms=sequence * 1000,
            ),
            payload=event_payload,
        )

    state = graphblocks_tui.TuiProtocolSession("run-1").apply_all(
        (
            event("RunStarted", 1, {"status": "running"}),
            event("AssistantDraftDelta", 2, {"delta": "Partial"}),
            event("ToolCallApprovalRequested", 3, {"approval_id": "approval-1"}),
            event("AssistantIncomplete", 4, {"reason": "policy_denied"}),
        )
    )
    screen = graphblocks_tui.workspace_assistant_screen(state)

    assert state.status == "running"
    assert state.assistant_state == "incomplete"
    assert state.assistant_text == "Partial"
    assert state.pending_actions == ("approval-1",)
    assert state.counters["ToolCallApprovalRequested"] == 1
    assert screen.screen_contract()["sections"][1]["rows"] == {
        "state": "incomplete",
        "text": "Partial",
    }


def test_tui_package_projects_job_progress_from_streaming_tool_results(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    def event(kind: str, sequence: int, payload: dict[str, object] | None = None):
        return graphblocks_client.ApplicationProtocolEvent.new(
            kind,
            graphblocks_client.ApplicationProtocolEventMetadata(
                event_id=f"event-{sequence}",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                sequence=sequence,
                occurred_at_unix_ms=sequence * 1000,
            ),
            payload=payload or {},
        )

    state = graphblocks_tui.TuiProtocolSession("run-1").apply_all(
        (
            event("RunStarted", 1, {"status": "running"}),
            event(
                "JobProgress",
                2,
                {
                    "tool_call_id": "tool-call-1",
                    "tool_result_sequence": 2,
                    "output": [
                        {
                            "kind": "text",
                            "text": "searching docs",
                            "metadata": {"trust_designation": "untrusted_external"},
                        }
                    ],
                },
            ),
            event("JobProgress", 3, {"tool_call_id": "tool-call-1", "message": "ranked 4 hits"}),
            event("JobProgress", 4, {"job_id": "embedding", "tool_result_sequence": 7}),
        )
    )
    screen = graphblocks_tui.workspace_assistant_screen(state)

    assert state.tool_progress == {"embedding": "sequence 7", "tool-call-1": "ranked 4 hits"}
    assert state.counters["JobProgress"] == 3
    assert screen.screen_contract()["sections"][3] == {
        "title": "Progress",
        "rows": {"embedding": "sequence 7", "tool-call-1": "ranked 4 hits"},
    }


def test_tui_package_ignores_boolean_tool_result_sequence_in_job_progress(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    event = graphblocks_client.ApplicationProtocolEvent.new(
        "JobProgress",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=1,
            occurred_at_unix_ms=1_000,
        ),
        payload={"job_id": "embedding", "tool_result_sequence": True},
    )

    state = graphblocks_tui.TuiProtocolSession("run-1").apply(event)

    assert state.tool_progress == {"embedding": "updated"}


def test_tui_package_projects_output_cutoff_events(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    def event(kind: str, sequence: int, payload: dict[str, object] | None = None):
        return graphblocks_client.ApplicationProtocolEvent.new(
            kind,
            graphblocks_client.ApplicationProtocolEventMetadata(
                event_id=f"event-{sequence}",
                protocol_version="graphblocks.app.v1",
                run_id="run-1",
                sequence=sequence,
                occurred_at_unix_ms=sequence * 1000,
            ),
            payload=payload or {},
        )

    state = graphblocks_tui.TuiProtocolSession("run-1").apply_all(
        (
            event("RunStarted", 1, {"status": "running"}),
            event("AssistantDraftDelta", 2, {"delta": "Sensitive draft"}),
            event(
                "OutputCutoff",
                3,
                {
                    "terminal_reason": "policy_denied",
                    "draft_disposition": "retract",
                    "last_client_delivered_sequence": 2,
                },
            ),
        )
    )
    screen = graphblocks_tui.workspace_assistant_screen(state)

    assert state.status == "policy_denied"
    assert state.assistant_state == "retracted"
    assert state.last_event == "OutputCutoff"
    assert state.counters["OutputCutoff"] == 1
    assert screen.screen_contract()["sections"][0]["rows"] == {
        "last_event": "OutputCutoff",
        "sequence": "3",
        "status": "policy_denied",
    }


def test_tui_package_projects_policy_stopped_run_event(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    event = graphblocks_client.ApplicationProtocolEvent.new(
        "RunPolicyStopped",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-policy-stopped",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=4,
            occurred_at_unix_ms=4_000,
        ),
        payload={"reason": "output policy denied"},
    )

    state = graphblocks_tui.TuiProtocolSession("run-1", status="running").apply(event)

    assert state.status == "policy_stopped"
    assert state.last_event == "RunPolicyStopped"
    assert state.counters["RunPolicyStopped"] == 1


def test_tui_package_projects_expired_run_event(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-client" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-tui" / "src"))
    graphblocks_client = importlib.import_module("graphblocks_client")
    graphblocks_tui = importlib.import_module("graphblocks_tui")

    event = graphblocks_client.ApplicationProtocolEvent.new(
        "RunExpired",
        graphblocks_client.ApplicationProtocolEventMetadata(
            event_id="event-expired",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=5,
            occurred_at_unix_ms=5_000,
        ),
        payload={"reason": "deadline exceeded"},
    )

    state = graphblocks_tui.TuiProtocolSession("run-1", status="running").apply(event)

    assert state.status == "expired"
    assert state.last_event == "RunExpired"
    assert state.counters["RunExpired"] == 1


def test_devtools_package_renders_dot_and_migration_plan(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-cli" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-devtools" / "src"))
    graphblocks_devtools = importlib.import_module("graphblocks_devtools")
    graph = graphblocks_devtools.DevGraph(
        graph_id="support",
        nodes=(
            graphblocks_devtools.DevGraphNode("begin", label="Begin"),
            graphblocks_devtools.DevGraphNode("agent", label="Agent"),
        ),
        edges=(graphblocks_devtools.DevGraphEdge("begin", "agent", label="messages"),),
    )
    plan = graphblocks_devtools.MigrationPlan(
        plan_id="rename-node",
        steps=(
            graphblocks_devtools.MigrationStep("rename", "Rename generate node to agent"),
            graphblocks_devtools.MigrationStep("verify", "Run profile TCK"),
        ),
    )

    assert graph.to_dot() == "\n".join(
        [
            'digraph "support" {',
            '  "begin" [label="Begin"];',
            '  "agent" [label="Agent"];',
            '  "begin" -> "agent" [label="messages"];',
            "}",
        ]
    )
    assert plan.plan_contract() == {
        "plan_id": "rename-node",
        "steps": [
            {"kind": "rename", "description": "Rename generate node to agent"},
            {"kind": "verify", "description": "Run profile TCK"},
        ],
    }


def test_devtools_package_builds_profile_summary_and_codegen_artifact(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-cli" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-devtools" / "src"))
    graphblocks_devtools = importlib.import_module("graphblocks_devtools")
    profile = graphblocks_devtools.ProfilingSummary.from_samples(
        profile_id="support-profile",
        samples=(
            graphblocks_devtools.ProfileSample("agent", 120),
            graphblocks_devtools.ProfileSample("agent", 80),
            graphblocks_devtools.ProfileSample("tools", 50),
        ),
    )
    artifact = graphblocks_devtools.CodegenArtifact(
        language="python",
        path="support_agent.py",
        content="def build(): pass",
    )

    assert profile.summary_contract() == {
        "profile_id": "support-profile",
        "total_ms": 250,
        "node_totals_ms": {"agent": 200, "tools": 50},
    }
    assert artifact.artifact_contract() == {
        "language": "python",
        "path": "support_agent.py",
        "content_digest": artifact.content_digest(),
    }


def test_devtools_package_builds_deterministic_diagnostic_bundle(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-cli" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-devtools" / "src"))
    graphblocks = importlib.import_module("graphblocks")
    graphblocks_devtools = importlib.import_module("graphblocks_devtools")

    bundle = graphblocks_devtools.DiagnosticBundle(
        bundle_id="release-checks",
        sections=(
            graphblocks_devtools.DiagnosticBundleSection(
                name="compiler",
                diagnostics=graphblocks.DiagnosticSet(
                    (
                        graphblocks.Diagnostic("GB1001", "node is not connected", "$.spec.nodes.agent", "warning"),
                        graphblocks.Diagnostic("GB0003", "metadata.name is required", "$.metadata.name"),
                    )
                ),
            ),
            graphblocks_devtools.DiagnosticBundleSection(
                name="package-doctor",
                diagnostics=(graphblocks.Diagnostic("GBPKG001", "missing package dependency", "$.packages[0]"),),
            ),
        ),
    )

    assert not bundle.ok
    assert bundle.bundle_contract() == {
        "bundle_id": "release-checks",
        "ok": False,
        "summary": {"error": 2, "warning": 1, "info": 0},
        "sections": [
            {
                "name": "compiler",
                "ok": False,
                "summary": {"error": 1, "warning": 1, "info": 0},
                "diagnostics": [
                    {
                        "code": "GB0003",
                        "severity": "error",
                        "path": "$.metadata.name",
                        "message": "metadata.name is required",
                    },
                    {
                        "code": "GB1001",
                        "severity": "warning",
                        "path": "$.spec.nodes.agent",
                        "message": "node is not connected",
                    },
                ],
            },
            {
                "name": "package-doctor",
                "ok": False,
                "summary": {"error": 1, "warning": 0, "info": 0},
                "diagnostics": [
                    {
                        "code": "GBPKG001",
                        "severity": "error",
                        "path": "$.packages[0]",
                        "message": "missing package dependency",
                    }
                ],
            },
        ],
    }
    assert bundle.content_digest().startswith("sha256:")
    assert bundle.content_digest() == graphblocks_devtools.DiagnosticBundle(
        bundle_id="release-checks",
        sections=tuple(reversed(bundle.sections)),
    ).content_digest()
