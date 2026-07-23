"""Typed Python authoring helpers for canonical Graph documents.

The runtime and compiler intentionally continue to consume the portable mapping
contract.  This module provides a typed authoring layer that materializes that
same contract at the boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
import re
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from .canonical import canonical_dumps, canonical_loads, normalize_graph
from .migration import GRAPH_API_VERSION
from .plugins import BlockCatalog, builtin_block_catalog
from .schema import SchemaId


T = TypeVar("T")
OutputsT = TypeVar("OutputsT")

_PRIMITIVE_TYPE_REFS = frozenset({"Any", "Boolean", "Bytes", "Integer", "Number", "Null", "String"})
_TYPE_CONSTRUCTOR_ARITY = {"List": 1, "Map": 2, "Optional": 1}
_MAX_PORT_TYPE_REF_DEPTH = 32
_MAX_PORT_TYPE_REF_LENGTH = 4_096


@dataclass(frozen=True)
class PortType(Generic[T]):
    """A GraphBlocks schema reference bound to a runtime marker class."""

    schema: str
    marker: type[T]

    def __post_init__(self) -> None:
        if not isinstance(self.schema, str):
            raise TypeError("port type schema must be a string")
        _validate_port_type_ref(self.schema)
        if not isinstance(self.marker, type):
            raise TypeError("port type marker must be a class")

    def matches(self, other: PortType[Any]) -> bool:
        return self.schema == other.schema and self.marker is other.marker

    def describe(self) -> str:
        return f"{self.schema} ({self.marker.__qualname__})"


@dataclass(frozen=True)
class GraphInput(Generic[T]):
    name: str
    port_type: PortType[T]
    _owner: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_name(self.name, "graph input")
        if not isinstance(self.port_type, PortType):
            raise TypeError("graph input port_type must be a PortType")

    @property
    def reference(self) -> str:
        return f"$input.{self.name}"


@dataclass(frozen=True)
class GraphOutput(Generic[T]):
    name: str
    port_type: PortType[T]
    _owner: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_name(self.name, "graph output")
        if not isinstance(self.port_type, PortType):
            raise TypeError("graph output port_type must be a PortType")

    @property
    def reference(self) -> str:
        return f"$output.{self.name}"


@dataclass(frozen=True)
class NodeOutput(Generic[T]):
    node_id: str
    name: str
    port_type: PortType[T]
    _owner: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_name(self.node_id, "node id")
        _validate_name(self.name, "block output")
        if not isinstance(self.port_type, PortType):
            raise TypeError("node output port_type must be a PortType")

    @property
    def reference(self) -> str:
        return f"{self.node_id}.{self.name}"


InputRef = GraphInput[T] | NodeOutput[T]


@dataclass(frozen=True)
class BoundBlock(Generic[OutputsT]):
    """A block definition whose typed inputs and configuration are bound."""

    block_id: str
    inputs: Mapping[str, InputRef[Any]]
    expected_inputs: Mapping[str, PortType[Any]]
    expected_outputs: Mapping[str, PortType[Any]]
    config: Mapping[str, object]
    _outputs: Callable[[str, object], OutputsT] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id.strip():
            raise ValueError("bound block id must be a non-empty string")
        if self.block_id != self.block_id.strip():
            raise ValueError("bound block id must not contain surrounding whitespace")
        for field_name, field_value in (
            ("inputs", self.inputs),
            ("expected_inputs", self.expected_inputs),
            ("expected_outputs", self.expected_outputs),
            ("config", self.config),
        ):
            if not isinstance(field_value, Mapping):
                raise TypeError(f"bound block {field_name} must be a mapping")
        if not callable(self._outputs):
            raise TypeError("bound block output factory must be callable")
        inputs = MappingProxyType(dict(self.inputs.items()))
        expected_inputs = MappingProxyType(dict(self.expected_inputs.items()))
        expected_outputs = MappingProxyType(dict(self.expected_outputs.items()))
        try:
            config = canonical_loads(canonical_dumps(self.config))
        except (TypeError, ValueError) as error:
            raise ValueError("bound block config must contain canonical JSON") from error
        if not isinstance(config, dict):
            raise TypeError("bound block config must be a mapping")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "expected_inputs", expected_inputs)
        object.__setattr__(self, "expected_outputs", expected_outputs)
        object.__setattr__(self, "config", _freeze_json(config))
        self._validate_input_contract()

    def _validate_input_contract(self) -> None:
        for name in self.expected_inputs:
            _validate_name(name, "expected block input")
        for name in self.expected_outputs:
            _validate_name(name, "expected block output")
        for contract in (self.expected_inputs, self.expected_outputs):
            for name, port_type in contract.items():
                if not isinstance(port_type, PortType):
                    raise TypeError(
                        f"expected block port {name!r} must be a PortType"
                    )
        for name, reference in self.inputs.items():
            _validate_name(name, "block input")
            if not isinstance(reference, GraphInput | NodeOutput):
                raise TypeError(f"block input {name!r} must be a typed port reference")
            if not isinstance(reference.port_type, PortType):
                raise TypeError(
                    f"block input {name!r} must use a PortType"
                )
        actual_names = set(self.inputs)
        expected_names = set(self.expected_inputs)
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        if missing or unexpected:
            raise ValueError(
                f"{self.block_id} input keys do not match contract: "
                f"missing={missing}; unexpected={unexpected}"
            )
        for name, expected_type in self.expected_inputs.items():
            reference = self.inputs[name]
            if not reference.port_type.matches(expected_type):
                raise TypeError(
                    f"{self.block_id} input {name!r} expects "
                    f"{expected_type.describe()}, got {reference.port_type.describe()}"
                )

    def _materialize(self, node_id: str, owner: object) -> tuple[dict[str, object], OutputsT]:
        self._validate_input_contract()
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
            node["config"] = _thaw_json(self.config)
        outputs = self._outputs(node_id, owner)
        actual_outputs = {output.name: output for output in _iter_node_outputs(outputs)}
        missing_outputs = sorted(set(self.expected_outputs) - set(actual_outputs))
        unexpected_outputs = sorted(set(actual_outputs) - set(self.expected_outputs))
        if missing_outputs or unexpected_outputs:
            raise ValueError(
                f"{self.block_id} output keys do not match contract: "
                f"missing={missing_outputs}; unexpected={unexpected_outputs}"
            )
        for name, expected_type in self.expected_outputs.items():
            actual_type = actual_outputs[name].port_type
            if not isinstance(actual_type, PortType):
                raise TypeError(
                    f"{self.block_id} output {name!r} must use a PortType"
                )
            if not actual_type.matches(expected_type):
                raise TypeError(
                    f"{self.block_id} output {name!r} expects "
                    f"{expected_type.describe()}, got {actual_type.describe()}"
                )
        return node, outputs


@dataclass(slots=True)
class GraphBuilder:
    """Build a portable Graph mapping while preserving typed port references."""

    name: str
    api_version: str = GRAPH_API_VERSION
    block_catalog: BlockCatalog = field(default_factory=builtin_block_catalog, repr=False)
    _owner: object = field(default_factory=object, init=False, repr=False)
    _inputs: dict[str, PortType[Any]] = field(default_factory=dict, init=False, repr=False)
    _outputs: dict[str, PortType[Any]] = field(default_factory=dict, init=False, repr=False)
    _nodes: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)
    _published_outputs: set[str] = field(default_factory=set, init=False, repr=False)
    _issued_inputs: dict[int, GraphInput[Any]] = field(default_factory=dict, init=False, repr=False)
    _issued_outputs: dict[int, GraphOutput[Any]] = field(default_factory=dict, init=False, repr=False)
    _issued_node_outputs: dict[int, NodeOutput[Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_name(self.name, "graph name")
        if not isinstance(self.api_version, str) or not self.api_version.strip():
            raise ValueError("graph api_version must be a non-empty string")
        if self.api_version != self.api_version.strip():
            raise ValueError(
                "graph api_version must not contain surrounding whitespace"
            )
        if not isinstance(self.block_catalog, BlockCatalog):
            raise TypeError("graph block_catalog must be a BlockCatalog")

    def input(self, name: str, port_type: PortType[T]) -> GraphInput[T]:
        _validate_name(name, "graph input")
        if not isinstance(port_type, PortType):
            raise TypeError("graph input port_type must be a PortType")
        SchemaId.parse(port_type.schema)
        if name in self._inputs:
            raise ValueError(f"graph input {name!r} is already declared")
        self._inputs[name] = port_type
        result = GraphInput(name, port_type, self._owner)
        self._issued_inputs[id(result)] = result
        return result

    def output(self, name: str, port_type: PortType[T]) -> GraphOutput[T]:
        _validate_name(name, "graph output")
        if not isinstance(port_type, PortType):
            raise TypeError("graph output port_type must be a PortType")
        SchemaId.parse(port_type.schema)
        if name in self._outputs:
            raise ValueError(f"graph output {name!r} is already declared")
        self._outputs[name] = port_type
        result = GraphOutput(name, port_type, self._owner)
        self._issued_outputs[id(result)] = result
        return result

    def add(self, node_id: str, block: BoundBlock[OutputsT]) -> OutputsT:
        _validate_name(node_id, "node id")
        if not isinstance(block, BoundBlock):
            raise TypeError("graph block must be a BoundBlock")
        if node_id in self._nodes:
            raise ValueError(f"node {node_id!r} is already declared")
        descriptor = self.block_catalog.get(block.block_id)
        if descriptor is None:
            raise ValueError(f"block {block.block_id!r} is not declared in the GraphBuilder catalog")
        descriptor_inputs = {port.name: port for port in descriptor.inputs}
        descriptor_outputs = {port.name: port for port in descriptor.outputs}
        for name, port_type in block.expected_inputs.items():
            catalog_port = descriptor_inputs.get(name)
            if catalog_port is None:
                raise ValueError(f"block {block.block_id!r} has no catalog input port {name!r}")
            if (
                catalog_port.type_ref not in {None, "Any"}
                and catalog_port.type_ref != port_type.schema
            ):
                raise TypeError(
                    f"block {block.block_id!r} input {name!r} declares {port_type.schema}, "
                    f"catalog expects {catalog_port.type_ref}"
                )
        missing_required_inputs = sorted(
            port.name
            for port in descriptor.inputs
            if port.required and port.name not in block.expected_inputs
        )
        if missing_required_inputs:
            raise ValueError(
                f"block {block.block_id!r} omits required catalog inputs {missing_required_inputs}"
            )
        for name, port_type in block.expected_outputs.items():
            catalog_port = descriptor_outputs.get(name)
            if catalog_port is None:
                raise ValueError(f"block {block.block_id!r} has no catalog output port {name!r}")
            if (
                catalog_port.type_ref not in {None, "Any"}
                and catalog_port.type_ref != port_type.schema
            ):
                raise TypeError(
                    f"block {block.block_id!r} output {name!r} declares {port_type.schema}, "
                    f"catalog expects {catalog_port.type_ref}"
                )
        missing_required_outputs = sorted(
            port.name
            for port in descriptor.outputs
            if port.required and port.name not in block.expected_outputs
        )
        if missing_required_outputs:
            raise ValueError(
                f"block {block.block_id!r} omits required catalog outputs {missing_required_outputs}"
            )
        for name, reference in block.inputs.items():
            if reference._owner is not self._owner:
                raise ValueError(f"block input {name!r} belongs to a different GraphBuilder")
            if isinstance(reference, GraphInput):
                if self._issued_inputs.get(id(reference)) is not reference:
                    raise ValueError(f"block input {name!r} is not an issued graph input")
            elif self._issued_node_outputs.get(id(reference)) is not reference:
                raise ValueError(f"block input {name!r} is not an issued node output")
        node, outputs = block._materialize(node_id, self._owner)
        issued_outputs = list(_iter_node_outputs(outputs))
        seen_names: set[str] = set()
        for output in issued_outputs:
            if output._owner is not self._owner or output.node_id != node_id:
                raise ValueError("block output factory returned a port for a different node or GraphBuilder")
            _validate_name(output.name, "block output")
            if output.name in seen_names:
                raise ValueError(f"block output {node_id}.{output.name} is duplicated")
            seen_names.add(output.name)
        for output in issued_outputs:
            self._issued_node_outputs[id(output)] = output
        self._nodes[node_id] = node
        return outputs

    def publish(self, output: GraphOutput[T], value: NodeOutput[T]) -> None:
        if not isinstance(output, GraphOutput):
            raise TypeError("published output must be a GraphOutput")
        if not isinstance(value, NodeOutput):
            raise TypeError("published value must be a NodeOutput")
        if output._owner is not self._owner:
            raise ValueError("graph output belongs to a different GraphBuilder")
        if self._issued_outputs.get(id(output)) is not output:
            raise ValueError("graph output was not issued by this GraphBuilder")
        declared_type = self._outputs.get(output.name)
        if declared_type is None or not output.port_type.matches(declared_type):
            raise ValueError("graph output does not match its declared interface contract")
        if value._owner is not self._owner:
            raise ValueError("node output belongs to a different GraphBuilder")
        if self._issued_node_outputs.get(id(value)) is not value:
            raise ValueError("node output was not issued by this GraphBuilder")
        if not value.port_type.matches(output.port_type):
            raise TypeError(
                f"graph output {output.name!r} expects {output.port_type.describe()}, "
                f"got {value.port_type.describe()}"
            )
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
        return normalize_graph(graph)


def _validate_name(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")


def _validate_port_type_ref(value: str, nesting_depth: int = 0) -> None:
    if len(value) > _MAX_PORT_TYPE_REF_LENGTH:
        raise ValueError(
            "port type reference must contain at most "
            f"{_MAX_PORT_TYPE_REF_LENGTH} characters"
        )
    if nesting_depth > _MAX_PORT_TYPE_REF_DEPTH:
        raise ValueError(
            "port type reference nesting must not exceed "
            f"{_MAX_PORT_TYPE_REF_DEPTH} levels"
        )
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise ValueError("port type reference must be non-empty and contain no whitespace")
    if value in _PRIMITIVE_TYPE_REFS:
        return
    if "<" not in value and ">" not in value:
        SchemaId.parse(value)
        return
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)<(.*)>", value)
    if match is None or match.group(1) not in _TYPE_CONSTRUCTOR_ARITY:
        raise ValueError(f"invalid port type reference {value!r}")
    arguments: list[str] = []
    depth = 0
    start = 0
    body = match.group(2)
    for index, character in enumerate(body):
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
            if depth < 0:
                raise ValueError(f"invalid port type reference {value!r}")
        elif character == "," and depth == 0:
            arguments.append(body[start:index])
            start = index + 1
    if depth != 0:
        raise ValueError(f"invalid port type reference {value!r}")
    arguments.append(body[start:])
    expected_arity = _TYPE_CONSTRUCTOR_ARITY[match.group(1)]
    if len(arguments) != expected_arity or any(not argument for argument in arguments):
        raise ValueError(f"invalid port type reference {value!r}")
    for argument in arguments:
        _validate_port_type_ref(argument, nesting_depth + 1)


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _iter_node_outputs(value: object, seen: set[int] | None = None) -> list[NodeOutput[Any]]:
    if isinstance(value, NodeOutput):
        return [value]
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return []
    seen.add(identity)
    if is_dataclass(value) and not isinstance(value, type):
        result: list[NodeOutput[Any]] = []
        for item in fields(value):
            result.extend(_iter_node_outputs(getattr(value, item.name), seen))
        return result
    if isinstance(value, Mapping):
        result = []
        for item in value.values():
            result.extend(_iter_node_outputs(item, seen))
        return result
    if isinstance(value, tuple | list):
        result = []
        for item in value:
            result.extend(_iter_node_outputs(item, seen))
        return result
    return []


__all__ = [
    "BoundBlock",
    "GraphBuilder",
    "GraphInput",
    "GraphOutput",
    "InputRef",
    "NodeOutput",
    "PortType",
]
