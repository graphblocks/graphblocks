from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json


class DashboardAssetError(ValueError):
    """Raised when a dashboard asset contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sorted_str_mapping(values: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(dict(values).items())}


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DashboardVariable:
    name: str
    query: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DashboardAssetError("dashboard variable name must not be empty")
        if not self.query.strip():
            raise DashboardAssetError("dashboard variable query must not be empty")

    def variable_contract(self) -> dict[str, str]:
        return {"name": self.name, "query": self.query}


@dataclass(frozen=True, slots=True)
class DashboardPanel:
    title: str
    query: str
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise DashboardAssetError("dashboard panel title must not be empty")
        if not self.query.strip():
            raise DashboardAssetError("dashboard panel query must not be empty")
        if self.unit is not None and not self.unit.strip():
            raise DashboardAssetError("dashboard panel unit must not be empty")

    def panel_contract(self) -> dict[str, str]:
        contract = {"title": self.title, "query": self.query}
        if self.unit is not None:
            contract["unit"] = self.unit
        return contract


@dataclass(frozen=True, slots=True)
class DashboardTemplate:
    name: str
    title: str
    panels: tuple[DashboardPanel, ...]
    variables: tuple[DashboardVariable, ...] = field(default_factory=tuple)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DashboardAssetError("dashboard template name must not be empty")
        if not self.title.strip():
            raise DashboardAssetError("dashboard template title must not be empty")
        if not self.panels:
            raise DashboardAssetError("dashboard template requires at least one panel")
        object.__setattr__(self, "panels", tuple(self.panels))
        object.__setattr__(self, "variables", tuple(self.variables))
        object.__setattr__(self, "metadata", _sorted_str_mapping(self.metadata))

    def dashboard_contract(self) -> dict[str, object]:
        contract: dict[str, object] = {
            "name": self.name,
            "title": self.title,
            "variables": [variable.variable_contract() for variable in self.variables],
            "panels": [panel.panel_contract() for panel in self.panels],
        }
        if self.metadata:
            contract["metadata"] = deepcopy(dict(self.metadata))
        return contract

    def content_digest(self) -> str:
        return _content_digest(self.dashboard_contract())


@dataclass(frozen=True, slots=True)
class SloRule:
    name: str
    objective: float
    indicator_query: str
    window: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DashboardAssetError("SLO name must not be empty")
        if not 0 < self.objective <= 1:
            raise DashboardAssetError("SLO objective must be in the interval (0, 1]")
        if not self.indicator_query.strip():
            raise DashboardAssetError("SLO indicator query must not be empty")
        if not self.window.strip():
            raise DashboardAssetError("SLO window must not be empty")
        object.__setattr__(self, "objective", float(self.objective))

    def rule_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "objective": self.objective,
            "indicatorQuery": self.indicator_query,
            "window": self.window,
        }

    def content_digest(self) -> str:
        return _content_digest(self.rule_contract())


@dataclass(frozen=True, slots=True)
class RunbookTemplate:
    runbook_id: str
    title: str
    steps: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.runbook_id.strip():
            raise DashboardAssetError("runbook_id must not be empty")
        if not self.title.strip():
            raise DashboardAssetError("runbook title must not be empty")
        steps = tuple(str(step) for step in self.steps)
        if any(not step.strip() for step in steps):
            raise DashboardAssetError("runbook steps must not be empty")
        object.__setattr__(self, "steps", steps)

    def runbook_contract(self) -> dict[str, object]:
        return {
            "id": self.runbook_id,
            "title": self.title,
            "steps": list(self.steps),
        }

    def content_digest(self) -> str:
        return _content_digest(self.runbook_contract())


def default_generation_dashboard() -> DashboardTemplate:
    return DashboardTemplate(
        name="graphblocks-generation",
        title="GraphBlocks Generation",
        variables=(
            DashboardVariable("release_id", "label_values(release_id)"),
        ),
        panels=(
            DashboardPanel(
                "Token Usage",
                'sum(rate(graphblocks_generation_usage_tokens_total{release_id="$release_id"}[5m])) by (token_type)',
                unit="tokens/sec",
            ),
            DashboardPanel(
                "Generation Timing",
                'avg(graphblocks_generation_timing_milliseconds{release_id="$release_id"}) by (phase)',
                unit="ms",
            ),
        ),
    )


def default_policy_tool_dashboard() -> DashboardTemplate:
    return DashboardTemplate(
        name="graphblocks-policy-tools",
        title="GraphBlocks Policy and Tools",
        variables=(
            DashboardVariable("release_id", "label_values(release_id)"),
        ),
        panels=(
            DashboardPanel(
                "Output Policy Decisions",
                'sum(rate(graphblocks_output_policy_decisions_total{release_id="$release_id"}[5m])) '
                "by (enforcement_point, disposition)",
                unit="decisions/sec",
            ),
            DashboardPanel(
                "Output Policy Cutoffs",
                'sum(rate(graphblocks_output_policy_cutoffs_total{release_id="$release_id"}[5m])) '
                "by (terminal_reason, draft_disposition)",
                unit="cutoffs/sec",
            ),
            DashboardPanel(
                "Tool Executions",
                'sum(rate(graphblocks_tool_executions_total{release_id="$release_id"}[5m])) '
                "by (tool_name, status)",
                unit="calls/sec",
            ),
            DashboardPanel(
                "Tool Execution Duration",
                'avg(graphblocks_tool_execution_duration_milliseconds{release_id="$release_id"}) '
                "by (tool_name, status)",
                unit="ms",
            ),
        ),
    )


__all__ = [
    "DashboardAssetError",
    "DashboardPanel",
    "DashboardTemplate",
    "DashboardVariable",
    "RunbookTemplate",
    "SloRule",
    "default_generation_dashboard",
    "default_policy_tool_dashboard",
]
