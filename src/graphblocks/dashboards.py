from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import math
from types import MappingProxyType


class DashboardAssetError(ValueError):
    """Raised when a dashboard asset contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _stable_string(
    owner: str,
    field_name: str,
    value: object,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DashboardAssetError(f"{owner} {field_name} must be a string")
    if not allow_empty and not value.strip():
        raise DashboardAssetError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise DashboardAssetError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DashboardAssetError(
            f"{owner} {field_name} must contain valid Unicode scalar values"
        ) from error
    return value


def _sorted_str_mapping(values: object) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        raise DashboardAssetError("dashboard template metadata must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized_key = _stable_string("dashboard template", "metadata key", key)
        normalized_value = _stable_string(
            "dashboard template",
            "metadata value",
            value,
            allow_empty=True,
        )
        normalized[normalized_key] = normalized_value
    return MappingProxyType(dict(sorted(normalized.items())))


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DashboardVariable:
    name: str
    query: str

    def __post_init__(self) -> None:
        _stable_string("dashboard variable", "name", self.name)
        _stable_string("dashboard variable", "query", self.query)

    def variable_contract(self) -> dict[str, str]:
        return {"name": self.name, "query": self.query}


@dataclass(frozen=True, slots=True)
class DashboardPanel:
    title: str
    query: str
    unit: str | None = None

    def __post_init__(self) -> None:
        _stable_string("dashboard panel", "title", self.title)
        _stable_string("dashboard panel", "query", self.query)
        if self.unit is not None:
            _stable_string("dashboard panel", "unit", self.unit)

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
        _stable_string("dashboard template", "name", self.name)
        _stable_string("dashboard template", "title", self.title)
        try:
            panels = tuple(self.panels)
            variables = tuple(self.variables)
        except TypeError as error:
            raise DashboardAssetError(
                "dashboard template panels and variables must be collections"
            ) from error
        if not panels:
            raise DashboardAssetError("dashboard template requires at least one panel")
        if any(not isinstance(panel, DashboardPanel) for panel in panels):
            raise DashboardAssetError(
                "dashboard template panels must contain DashboardPanel records"
            )
        if any(not isinstance(variable, DashboardVariable) for variable in variables):
            raise DashboardAssetError(
                "dashboard template variables must contain DashboardVariable records"
            )
        variable_names = [variable.name for variable in variables]
        if len(set(variable_names)) != len(variable_names):
            raise DashboardAssetError(
                "dashboard template variable names must be unique"
            )
        object.__setattr__(self, "panels", panels)
        object.__setattr__(self, "variables", variables)
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
        _stable_string("SLO", "name", self.name)
        if isinstance(self.objective, bool) or not isinstance(
            self.objective,
            (int, float, Decimal),
        ):
            raise DashboardAssetError("SLO objective must be numeric")
        objective = float(self.objective)
        if not math.isfinite(objective):
            raise DashboardAssetError("SLO objective must be finite")
        if not 0 < objective <= 1:
            raise DashboardAssetError("SLO objective must be in the interval (0, 1]")
        _stable_string("SLO", "indicator query", self.indicator_query)
        _stable_string("SLO", "window", self.window)
        object.__setattr__(self, "objective", objective)

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
        _stable_string("runbook", "runbook_id", self.runbook_id)
        _stable_string("runbook", "title", self.title)
        if isinstance(self.steps, (str, bytes, bytearray)):
            raise DashboardAssetError("runbook steps must be a collection")
        try:
            steps = tuple(self.steps)
        except TypeError as error:
            raise DashboardAssetError("runbook steps must be a collection") from error
        if not steps:
            raise DashboardAssetError("runbook requires at least one step")
        for step in steps:
            _stable_string("runbook", "step", step)
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
