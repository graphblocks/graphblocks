from __future__ import annotations

from dataclasses import dataclass, field
import hashlib


class DevtoolsContractError(ValueError):
    """Raised when a developer tooling contract is invalid."""


def _content_digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _dot_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class DevGraphNode:
    node_id: str
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise DevtoolsContractError("node_id must not be empty")

    def dot_line(self) -> str:
        label = self.label or self.node_id
        return f"  {_dot_quote(self.node_id)} [label={_dot_quote(label)}];"


@dataclass(frozen=True, slots=True)
class DevGraphEdge:
    source: str
    target: str
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise DevtoolsContractError("edge source must not be empty")
        if not self.target.strip():
            raise DevtoolsContractError("edge target must not be empty")

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
        if not self.graph_id.strip():
            raise DevtoolsContractError("graph_id must not be empty")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))

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
        if not self.kind.strip():
            raise DevtoolsContractError("migration step kind must not be empty")
        if not self.description.strip():
            raise DevtoolsContractError("migration step description must not be empty")

    def step_contract(self) -> dict[str, str]:
        return {"kind": self.kind, "description": self.description}


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    plan_id: str
    steps: tuple[MigrationStep, ...]

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise DevtoolsContractError("migration plan_id must not be empty")
        object.__setattr__(self, "steps", tuple(self.steps))

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
        if not self.node_id.strip():
            raise DevtoolsContractError("profile sample node_id must not be empty")
        if self.elapsed_ms < 0:
            raise DevtoolsContractError("profile sample elapsed_ms must not be negative")


@dataclass(frozen=True, slots=True)
class ProfilingSummary:
    profile_id: str
    node_totals_ms: dict[str, int]

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise DevtoolsContractError("profile_id must not be empty")
        object.__setattr__(self, "node_totals_ms", dict(sorted(self.node_totals_ms.items())))

    @classmethod
    def from_samples(cls, *, profile_id: str, samples: tuple[ProfileSample, ...]) -> ProfilingSummary:
        totals: dict[str, int] = {}
        for sample in samples:
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
        if not self.language.strip():
            raise DevtoolsContractError("codegen language must not be empty")
        if not self.path.strip():
            raise DevtoolsContractError("codegen path must not be empty")

    def content_digest(self) -> str:
        return _content_digest(self.content)

    def artifact_contract(self) -> dict[str, str]:
        return {
            "language": self.language,
            "path": self.path,
            "content_digest": self.content_digest(),
        }


__all__ = [
    "CodegenArtifact",
    "DevGraph",
    "DevGraphEdge",
    "DevGraphNode",
    "DevtoolsContractError",
    "MigrationPlan",
    "MigrationStep",
    "ProfileSample",
    "ProfilingSummary",
]
