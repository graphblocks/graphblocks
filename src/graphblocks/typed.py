"""Typed Python authoring helpers for canonical Graph documents.

The runtime and compiler intentionally continue to consume the portable mapping
contract.  This module provides a typed authoring layer that materializes that
same contract at the boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .canonical import canonical_dumps
from .schema import SchemaId


T = TypeVar("T")
OutputsT = TypeVar("OutputsT")


@dataclass(frozen=True)
class PortType(Generic[T]):
    """A GraphBlocks schema reference carrying a Python-only value type."""

    schema: str

    def __post_init__(self) -> None:
        if not isinstance(self.schema, str):
            raise TypeError("port type schema must be a string")
        SchemaId.parse(self.schema)


@dataclass(frozen=True)
class GraphInput(Generic[T]):
    name: str
    port_type: PortType[T]
    _owner: object = field(repr=False, compare=False)

    @property
    def reference(self) -> str:
        return f"$input.{self.name}"


@dataclass(frozen=True)
class GraphOutput(Generic[T]):
    name: str
    port_type: PortType[T]
    _owner: object = field(repr=False, compare=False)

    @property
    def reference(self) -> str:
        return f"$output.{self.name}"


@dataclass(frozen=True)
class NodeOutput(Generic[T]):
    node_id: str
    name: str
    _owner: object = field(repr=False, compare=False)

    @property
    def reference(self) -> str:
        return f"{self.node_id}.{self.name}"


InputRef = GraphInput[T] | NodeOutput[T]


@dataclass(frozen=True)
class BoundBlock(Generic[OutputsT]):
    """A block definition whose typed inputs and configuration are bound."""

    block_id: str
    inputs: Mapping[str, InputRef[Any]]
    config: Mapping[str, object]
    _outputs: Callable[[str, object], OutputsT] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id.strip():
            raise ValueError("bound block id must be a non-empty string")
        if self.block_id != self.block_id.strip():
            raise ValueError("bound block id must not contain surrounding whitespace")
        for name, reference in self.inputs.items():
            _validate_name(name, "block input")
            if not isinstance(reference, GraphInput | NodeOutput):
                raise TypeError(f"block input {name!r} must be a typed port reference")
        canonical_dumps(dict(self.config))

    def _materialize(self, node_id: str, owner: object) -> tuple[dict[str, object], OutputsT]:
        for name, reference in self.inputs.items():
            if reference._owner is not owner:
                raise ValueError(
                    f"block input {name!r} belongs to a different GraphBuilder"
                )
        node: dict[str, object] = {
            "block": self.block_id,
            "inputs": {name: reference.reference for name, reference in self.inputs.items()},
        }
        if self.config:
            node["config"] = deepcopy(dict(self.config))
        return node, self._outputs(node_id, owner)


@dataclass(slots=True)
class GraphBuilder:
    """Build a portable Graph mapping while preserving typed port references."""

    name: str
    api_version: str = "graphblocks.ai/v1alpha3"
    _owner: object = field(default_factory=object, init=False, repr=False)
    _inputs: dict[str, PortType[Any]] = field(default_factory=dict, init=False, repr=False)
    _outputs: dict[str, PortType[Any]] = field(default_factory=dict, init=False, repr=False)
    _nodes: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)
    _published_outputs: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_name(self.name, "graph name")
        if not isinstance(self.api_version, str) or not self.api_version.strip():
            raise ValueError("graph api_version must be a non-empty string")

    def input(self, name: str, port_type: PortType[T]) -> GraphInput[T]:
        _validate_name(name, "graph input")
        if name in self._inputs:
            raise ValueError(f"graph input {name!r} is already declared")
        self._inputs[name] = port_type
        return GraphInput(name, port_type, self._owner)

    def output(self, name: str, port_type: PortType[T]) -> GraphOutput[T]:
        _validate_name(name, "graph output")
        if name in self._outputs:
            raise ValueError(f"graph output {name!r} is already declared")
        self._outputs[name] = port_type
        return GraphOutput(name, port_type, self._owner)

    def add(self, node_id: str, block: BoundBlock[OutputsT]) -> OutputsT:
        _validate_name(node_id, "node id")
        if node_id in self._nodes:
            raise ValueError(f"node {node_id!r} is already declared")
        node, outputs = block._materialize(node_id, self._owner)
        self._nodes[node_id] = node
        return outputs

    def publish(self, output: GraphOutput[T], value: NodeOutput[T]) -> None:
        if output._owner is not self._owner:
            raise ValueError("graph output belongs to a different GraphBuilder")
        if value._owner is not self._owner:
            raise ValueError("node output belongs to a different GraphBuilder")
        node = self._nodes.get(value.node_id)
        if node is None:
            raise ValueError(f"node {value.node_id!r} is not declared")
        if output.name in self._published_outputs:
            raise ValueError(f"graph output {output.name!r} is already published")
        node_outputs = node.setdefault("outputs", {})
        assert isinstance(node_outputs, dict)
        if value.name in node_outputs:
            raise ValueError(
                f"node output {value.node_id}.{value.name} is already published"
            )
        node_outputs[value.name] = output.reference
        self._published_outputs.add(output.name)

    def build(self) -> dict[str, Any]:
        unpublished = set(self._outputs) - self._published_outputs
        if unpublished:
            names = ", ".join(sorted(unpublished))
            raise ValueError(f"graph outputs are not published: {names}")
        graph: dict[str, Any] = {
            "apiVersion": self.api_version,
            "kind": "Graph",
            "metadata": {"name": self.name},
            "spec": {
                "interface": {
                    "inputs": {
                        name: port_type.schema for name, port_type in self._inputs.items()
                    },
                    "outputs": {
                        name: port_type.schema for name, port_type in self._outputs.items()
                    },
                },
                "nodes": deepcopy(self._nodes),
            },
        }
        canonical_dumps(graph)
        return graph


def _validate_name(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")


__all__ = [
    "BoundBlock",
    "GraphBuilder",
    "GraphInput",
    "GraphOutput",
    "InputRef",
    "NodeOutput",
    "PortType",
]
