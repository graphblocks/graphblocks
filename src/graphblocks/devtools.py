from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Iterable

from graphblocks.diagnostics import Diagnostic, DiagnosticSet, Severity


_MAX_U64 = (1 << 64) - 1


class DevtoolsContractError(ValueError):
    """Raised when a developer tooling contract is invalid."""


def _stable_string(
    owner: str,
    field_name: str,
    value: object,
    *,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise DevtoolsContractError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise DevtoolsContractError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise DevtoolsContractError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    if "\0" in value:
        raise DevtoolsContractError(f"{owner} {field_name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DevtoolsContractError(
            f"{owner} {field_name} must contain valid Unicode scalar values"
        ) from error
    return value


def _content_digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_content_digest(content: object) -> str:
    return _content_digest(
        json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _dot_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return '"' + escaped + '"'


def _diagnostic_summary(diagnostics: Iterable[Diagnostic]) -> dict[Severity, int]:
    summary: dict[Severity, int] = {"error": 0, "warning": 0, "info": 0}
    for diagnostic in diagnostics:
        summary[diagnostic.severity] += 1
    return summary


@dataclass(frozen=True, slots=True)
class DevGraphNode:
    node_id: str
    label: str | None = None

    def __post_init__(self) -> None:
        _stable_string("graph node", "node_id", self.node_id)
        _stable_string("graph node", "label", self.label, optional=True)

    def dot_line(self) -> str:
        label = self.label or self.node_id
        return f"  {_dot_quote(self.node_id)} [label={_dot_quote(label)}];"


@dataclass(frozen=True, slots=True)
class DevGraphEdge:
    source: str
    target: str
    label: str | None = None

    def __post_init__(self) -> None:
        _stable_string("graph edge", "source", self.source)
        _stable_string("graph edge", "target", self.target)
        _stable_string("graph edge", "label", self.label, optional=True)

    def dot_line(self) -> str:
        line = f"  {_dot_quote(self.source)} -> {_dot_quote(self.target)}"
        if self.label is not None:
            line += f" [label={_dot_quote(self.label)}]"
        return line + ";"


@dataclass(frozen=True, slots=True)
class DevGraph:
    graph_id: str
    nodes: tuple[DevGraphNode, ...] = field(default_factory=tuple)
    edges: tuple[DevGraphEdge, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _stable_string("developer graph", "graph_id", self.graph_id)
        try:
            nodes = tuple(self.nodes)
            edges = tuple(self.edges)
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError(
                "developer graph nodes and edges must be collections"
            ) from error
        if any(not isinstance(node, DevGraphNode) for node in nodes):
            raise DevtoolsContractError(
                "developer graph nodes must contain DevGraphNode records"
            )
        if any(not isinstance(edge, DevGraphEdge) for edge in edges):
            raise DevtoolsContractError(
                "developer graph edges must contain DevGraphEdge records"
            )
        node_ids = [node.node_id for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise DevtoolsContractError("developer graph node_id values must be unique")
        if len(set(edges)) != len(edges):
            raise DevtoolsContractError("developer graph edges must be unique")
        known_node_ids = set(node_ids)
        if any(
            edge.source not in known_node_ids or edge.target not in known_node_ids
            for edge in edges
        ):
            raise DevtoolsContractError(
                "developer graph edges must reference declared nodes"
            )
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)

    def to_dot(self) -> str:
        lines = [f"digraph {_dot_quote(self.graph_id)} {{"]
        lines.extend(node.dot_line() for node in self.nodes)
        lines.extend(edge.dot_line() for edge in self.edges)
        lines.append("}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class MigrationStep:
    kind: str
    description: str

    def __post_init__(self) -> None:
        _stable_string("migration step", "kind", self.kind)
        _stable_string("migration step", "description", self.description)

    def step_contract(self) -> dict[str, str]:
        return {"kind": self.kind, "description": self.description}


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    plan_id: str
    steps: tuple[MigrationStep, ...]

    def __post_init__(self) -> None:
        _stable_string("migration plan", "plan_id", self.plan_id)
        try:
            steps = tuple(self.steps)
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError("migration plan steps must be a collection") from error
        if any(not isinstance(step, MigrationStep) for step in steps):
            raise DevtoolsContractError(
                "migration plan steps must contain MigrationStep records"
            )
        object.__setattr__(self, "steps", steps)

    def plan_contract(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "steps": [step.step_contract() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class ProfileSample:
    node_id: str
    elapsed_ms: int

    def __post_init__(self) -> None:
        _stable_string("profile sample", "node_id", self.node_id)
        if not isinstance(self.elapsed_ms, int) or isinstance(self.elapsed_ms, bool):
            raise DevtoolsContractError("profile sample elapsed_ms must be an integer")
        if self.elapsed_ms < 0:
            raise DevtoolsContractError("profile sample elapsed_ms must not be negative")
        if self.elapsed_ms > _MAX_U64:
            raise DevtoolsContractError(
                f"profile sample elapsed_ms must not exceed {_MAX_U64}"
            )


@dataclass(frozen=True, slots=True)
class ProfilingSummary:
    profile_id: str
    node_totals_ms: Mapping[str, int]

    def __post_init__(self) -> None:
        _stable_string("profiling summary", "profile_id", self.profile_id)
        if not isinstance(self.node_totals_ms, Mapping):
            raise DevtoolsContractError(
                "profiling summary node_totals_ms must be a mapping"
            )
        try:
            items = tuple(self.node_totals_ms.items())
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError(
                "profiling summary node_totals_ms must be a stable mapping"
            ) from error
        normalized: dict[str, int] = {}
        for node_id, elapsed_ms in items:
            normalized_node_id = _stable_string(
                "profiling summary",
                "node_totals_ms key",
                node_id,
            )
            assert normalized_node_id is not None
            if normalized_node_id in normalized:
                raise DevtoolsContractError(
                    "profiling summary node_totals_ms keys must be unique"
                )
            if not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool):
                raise DevtoolsContractError(
                    "profiling summary node totals must be integers"
                )
            if elapsed_ms < 0:
                raise DevtoolsContractError(
                    "profiling summary node totals must not be negative"
                )
            if elapsed_ms > _MAX_U64:
                raise DevtoolsContractError(
                    f"profiling summary node totals must not exceed {_MAX_U64}"
                )
            normalized[normalized_node_id] = elapsed_ms
        if sum(normalized.values()) > _MAX_U64:
            raise DevtoolsContractError(
                f"profiling summary total_ms must not exceed {_MAX_U64}"
            )
        object.__setattr__(
            self,
            "node_totals_ms",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    @classmethod
    def from_samples(cls, *, profile_id: str, samples: tuple[ProfileSample, ...]) -> ProfilingSummary:
        try:
            normalized_samples = tuple(samples)
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError("profile samples must be a collection") from error
        if any(not isinstance(sample, ProfileSample) for sample in normalized_samples):
            raise DevtoolsContractError(
                "profile samples must contain ProfileSample records"
            )
        totals: dict[str, int] = {}
        for sample in normalized_samples:
            totals[sample.node_id] = totals.get(sample.node_id, 0) + sample.elapsed_ms
        return cls(profile_id=profile_id, node_totals_ms=totals)

    def summary_contract(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "total_ms": sum(self.node_totals_ms.values()),
            "node_totals_ms": dict(self.node_totals_ms),
        }


@dataclass(frozen=True, slots=True)
class CodegenArtifact:
    language: str
    path: str
    content: str

    def __post_init__(self) -> None:
        _stable_string("codegen artifact", "language", self.language)
        path = _stable_string("codegen artifact", "path", self.path)
        assert path is not None
        if "\\" in path:
            raise DevtoolsContractError(
                "codegen artifact path must be a relative POSIX path"
            )
        parsed_path = PurePosixPath(path)
        if (
            parsed_path.is_absolute()
            or not parsed_path.parts
            or any(part in {".", ".."} for part in parsed_path.parts)
            or parsed_path.as_posix() != path
        ):
            raise DevtoolsContractError(
                "codegen artifact path must be a relative normalized path"
            )
        if not isinstance(self.content, str):
            raise DevtoolsContractError("codegen artifact content must be a string")
        try:
            self.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise DevtoolsContractError(
                "codegen artifact content must contain valid Unicode scalar values"
            ) from error

    def content_digest(self) -> str:
        return _content_digest(self.content)

    def artifact_contract(self) -> dict[str, str]:
        return {
            "language": self.language,
            "path": self.path,
            "content_digest": self.content_digest(),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticBundleSection:
    name: str
    diagnostics: DiagnosticSet | tuple[Diagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _stable_string("diagnostic bundle section", "name", self.name)
        raw_diagnostics = (
            self.diagnostics.diagnostics
            if isinstance(self.diagnostics, DiagnosticSet)
            else self.diagnostics
        )
        try:
            diagnostics = tuple(raw_diagnostics)
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError(
                "diagnostic bundle section diagnostics must be a collection"
            ) from error
        if any(not isinstance(diagnostic, Diagnostic) for diagnostic in diagnostics):
            raise DevtoolsContractError(
                "diagnostic bundle section diagnostics must contain Diagnostic records"
            )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    diagnostics,
                    key=lambda item: (item.severity, item.code, item.path, item.message),
                )
            ),
        )

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def summary(self) -> dict[Severity, int]:
        return _diagnostic_summary(self.diagnostics)

    def section_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "summary": self.summary(),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class DiagnosticBundle:
    bundle_id: str
    sections: tuple[DiagnosticBundleSection, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _stable_string("diagnostic bundle", "bundle_id", self.bundle_id)
        try:
            sections = tuple(self.sections)
        except (TypeError, RuntimeError) as error:
            raise DevtoolsContractError(
                "diagnostic bundle sections must be a collection"
            ) from error
        if any(not isinstance(section, DiagnosticBundleSection) for section in sections):
            raise DevtoolsContractError(
                "diagnostic bundle sections must contain DiagnosticBundleSection records"
            )
        section_names = [section.name for section in sections]
        if len(set(section_names)) != len(section_names):
            raise DevtoolsContractError(
                "diagnostic bundle section names must be unique"
            )
        object.__setattr__(
            self,
            "sections",
            tuple(sorted(sections, key=lambda item: item.name)),
        )

    @property
    def ok(self) -> bool:
        return all(section.ok for section in self.sections)

    def summary(self) -> dict[Severity, int]:
        summary: dict[Severity, int] = {"error": 0, "warning": 0, "info": 0}
        for section in self.sections:
            for severity, count in section.summary().items():
                summary[severity] += count
        return summary

    def bundle_contract(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "ok": self.ok,
            "summary": self.summary(),
            "sections": [section.section_contract() for section in self.sections],
        }

    def content_digest(self) -> str:
        return _canonical_content_digest(self.bundle_contract())


__all__ = [
    "CodegenArtifact",
    "DevGraph",
    "DevGraphEdge",
    "DevGraphNode",
    "DevtoolsContractError",
    "DiagnosticBundle",
    "DiagnosticBundleSection",
    "MigrationPlan",
    "MigrationStep",
    "ProfileSample",
    "ProfilingSummary",
]
