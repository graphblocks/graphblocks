from __future__ import annotations

import importlib
from pathlib import Path


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
