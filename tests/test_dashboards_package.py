from __future__ import annotations

import importlib
import pickle
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _import_dashboards(monkeypatch):
    return importlib.import_module("graphblocks.dashboards")


def test_dashboards_package_builds_default_generation_dashboard(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)

    dashboard = graphblocks_dashboards.default_generation_dashboard()
    contract = dashboard.dashboard_contract()

    assert contract["name"] == "graphblocks-generation"
    assert contract["title"] == "GraphBlocks Generation"
    assert contract["variables"] == [{"name": "release_id", "query": "label_values(release_id)"}]
    assert contract["panels"] == [
        {
            "title": "Token Usage",
            "query": 'sum(rate(graphblocks_generation_usage_tokens_total{release_id="$release_id"}[5m])) by (token_type)',
            "unit": "tokens/sec",
        },
        {
            "title": "Generation Timing",
            "query": 'avg(graphblocks_generation_timing_milliseconds{release_id="$release_id"}) by (phase)',
            "unit": "ms",
        },
    ]
    assert dashboard.content_digest().startswith("sha256:")


def test_dashboards_package_builds_policy_tool_dashboard(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)

    dashboard = graphblocks_dashboards.default_policy_tool_dashboard()
    contract = dashboard.dashboard_contract()

    assert contract["name"] == "graphblocks-policy-tools"
    assert contract["title"] == "GraphBlocks Policy and Tools"
    assert contract["variables"] == [{"name": "release_id", "query": "label_values(release_id)"}]
    assert contract["panels"] == [
        {
            "title": "Output Policy Decisions",
            "query": 'sum(rate(graphblocks_output_policy_decisions_total{release_id="$release_id"}[5m])) '
            "by (enforcement_point, disposition)",
            "unit": "decisions/sec",
        },
        {
            "title": "Output Policy Cutoffs",
            "query": 'sum(rate(graphblocks_output_policy_cutoffs_total{release_id="$release_id"}[5m])) '
            "by (terminal_reason, draft_disposition)",
            "unit": "cutoffs/sec",
        },
        {
            "title": "Tool Executions",
            "query": 'sum(rate(graphblocks_tool_executions_total{release_id="$release_id"}[5m])) '
            "by (tool_name, status)",
            "unit": "calls/sec",
        },
        {
            "title": "Tool Execution Duration",
            "query": 'avg(graphblocks_tool_execution_duration_milliseconds{release_id="$release_id"}) '
            "by (tool_name, status)",
            "unit": "ms",
        },
    ]
    assert dashboard.content_digest().startswith("sha256:")
    assert "default_policy_tool_dashboard" in graphblocks_dashboards.__all__


def test_dashboards_package_builds_slo_and_runbook_contracts(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)
    slo = graphblocks_dashboards.SloRule(
        name="generation-latency",
        objective=0.99,
        indicator_query="graphblocks_generation_timing_milliseconds < 30000",
        window="30d",
    )
    runbook = graphblocks_dashboards.RunbookTemplate(
        runbook_id="generation-latency",
        title="Generation latency high",
        steps=("Check provider latency", "Inspect worker queue depth"),
    )

    assert slo.rule_contract() == {
        "name": "generation-latency",
        "objective": 0.99,
        "indicatorQuery": "graphblocks_generation_timing_milliseconds < 30000",
        "window": "30d",
    }
    assert runbook.runbook_contract() == {
        "id": "generation-latency",
        "title": "Generation latency high",
        "steps": ["Check provider latency", "Inspect worker queue depth"],
    }


def test_dashboards_template_digest_is_stable_across_metadata_order(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)
    panel = graphblocks_dashboards.DashboardPanel("Token Usage", "sum(tokens)", unit="tokens/sec")
    left = graphblocks_dashboards.DashboardTemplate(
        name="tokens",
        title="Tokens",
        panels=(panel,),
        metadata={"b": "2", "a": "1"},
    )
    right = graphblocks_dashboards.DashboardTemplate(
        name="tokens",
        title="Tokens",
        panels=(panel,),
        metadata={"a": "1", "b": "2"},
    )

    assert left.content_digest() == right.content_digest()


def test_dashboard_records_reject_coercion_and_ambiguous_variables(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)
    panel = graphblocks_dashboards.DashboardPanel("Token Usage", "sum(tokens)")
    variable = graphblocks_dashboards.DashboardVariable("release_id", "label_values(release_id)")

    with pytest.raises(
        graphblocks_dashboards.DashboardAssetError,
        match="variable name must be a string",
    ):
        graphblocks_dashboards.DashboardVariable(1, "query")  # type: ignore[arg-type]
    with pytest.raises(
        graphblocks_dashboards.DashboardAssetError,
        match="panels must contain DashboardPanel",
    ):
        graphblocks_dashboards.DashboardTemplate(
            "tokens",
            "Tokens",
            panels=(object(),),
        )
    with pytest.raises(
        graphblocks_dashboards.DashboardAssetError,
        match="variable names must be unique",
    ):
        graphblocks_dashboards.DashboardTemplate(
            "tokens",
            "Tokens",
            panels=(panel,),
            variables=(variable, variable),
        )
    with pytest.raises(
        graphblocks_dashboards.DashboardAssetError,
        match="metadata key must be a string",
    ):
        graphblocks_dashboards.DashboardTemplate(
            "tokens",
            "Tokens",
            panels=(panel,),
            metadata={1: "coerced"},  # type: ignore[dict-item]
        )


def test_dashboard_metadata_is_immutable_and_slo_numbers_are_strict(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)
    metadata = {"team": "runtime"}
    template = graphblocks_dashboards.DashboardTemplate(
        "tokens",
        "Tokens",
        panels=(graphblocks_dashboards.DashboardPanel("Token Usage", "sum(tokens)"),),
        metadata=metadata,
    )
    digest = template.content_digest()
    metadata["team"] = "mutated"

    assert template.metadata == {"team": "runtime"}
    assert template.content_digest() == digest
    with pytest.raises(TypeError):
        template.metadata["team"] = "mutated"
    for objective, message in (
        (True, "must be numeric"),
        ("0.99", "must be numeric"),
        (float("nan"), "must be finite"),
    ):
        with pytest.raises(graphblocks_dashboards.DashboardAssetError, match=message):
            graphblocks_dashboards.SloRule(
                "availability",
                objective,  # type: ignore[arg-type]
                "sum(up)",
                "30d",
            )


def test_dashboard_metadata_rejects_duplicate_mapping_items(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)

    class DuplicateItemsDict(dict):
        def items(self):
            return (("team", "runtime"), ("team", "platform"))

    with pytest.raises(
        graphblocks_dashboards.DashboardAssetError,
        match="duplicate metadata key",
    ):
        graphblocks_dashboards.DashboardTemplate(
            "tokens",
            "Tokens",
            panels=(
                graphblocks_dashboards.DashboardPanel(
                    "Token Usage",
                    "sum(tokens)",
                ),
            ),
            metadata=DuplicateItemsDict(),
        )


def test_dashboard_template_round_trips_immutable_metadata(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)
    template = graphblocks_dashboards.DashboardTemplate(
        "tokens",
        "Tokens",
        panels=(
            graphblocks_dashboards.DashboardPanel(
                "Token Usage",
                "sum(tokens)",
            ),
        ),
        metadata={"team": "runtime"},
    )

    restored = pickle.loads(pickle.dumps(template))

    assert restored == template
    assert restored.content_digest() == template.content_digest()
    with pytest.raises(TypeError):
        restored.metadata["team"] = "mutated"


def test_runbook_rejects_scalar_empty_and_coerced_steps(monkeypatch) -> None:
    graphblocks_dashboards = _import_dashboards(monkeypatch)

    for steps, message in (
        ("restart", "steps must be a collection"),
        ((), "requires at least one step"),
        ((1,), "step must be a string"),
    ):
        with pytest.raises(graphblocks_dashboards.DashboardAssetError, match=message):
            graphblocks_dashboards.RunbookTemplate(
                "restart",
                "Restart service",
                steps,  # type: ignore[arg-type]
            )
