from __future__ import annotations

import importlib
from pathlib import Path


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
