from __future__ import annotations

import importlib.metadata
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
import yaml

from .canonical import _reject_duplicate_keys
from .diagnostics import Diagnostic, DiagnosticSet
from .loader import _DuplicateKeySafeLoader
from .migration import (
    MigrationError,
    _migrate_document_unchecked,
    migrate_document,
)
from .schema import SchemaId, SchemaIdError, resource_schema_errors

STATIC_MANIFEST_NAMES = {
    "graphblocks-plugin.yaml",
    "graphblocks-plugin.yml",
    "graphblocks-plugin.json",
    "graphblocks_plugin.yaml",
    "graphblocks_plugin.yml",
    "graphblocks_plugin.json",
    ".graphblocks/plugin.yaml",
    ".graphblocks/plugin.yml",
    ".graphblocks/plugin.json",
}
_PRIMITIVE_TYPE_REFS = frozenset({"Any", "Boolean", "Bytes", "Integer", "Number", "Null", "String"})
_TYPE_CONSTRUCTOR_ARITY = {"List": 1, "Map": 2, "Optional": 1}
_MAX_TYPE_REF_DEPTH = 32
_MAX_CONFIG_SCHEMA_DEPTH = 64
_MAX_CONFIG_SCHEMA_NODES = 10_000
_OUTPUT_REQUIREDNESS_PHASES = frozenset({"initial", "resumed"})
_OUTPUT_REQUIREDNESS_OPERATORS = frozenset(
    {"configEquals", "phase", "all", "any", "not"}
)
_MAX_OUTPUT_REQUIREDNESS_DEPTH = 16
_MAX_OUTPUT_REQUIREDNESS_OPERANDS = 16
_ENDPOINT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_MISSING = object()
_STABLE_CORE_BLOCK_IDS = frozenset(
    {
        "control.map@2",
        "control.select@1",
        "model.generate@1",
        "prompt.render@1",
    }
)
_STABLE_CONTROL_MAP_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "block": {
            "type": "string",
            "pattern": r"^\S+@[1-9][0-9]*$",
        },
        "inputName": {"type": "string", "minLength": 1},
        "outputName": {"type": "string", "minLength": 1},
        "config": {"type": "object"},
        "onError": {"enum": ["fail_fast", "collect"]},
    },
    "required": ["block"],
    "additionalProperties": False,
}


def _block_capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("capabilities must be a list")
    capabilities: list[str] = []
    for capability in value:
        if (
            not isinstance(capability, str)
            or not capability
            or any(character.isspace() for character in capability)
        ):
            raise ValueError(
                "capabilities must contain non-empty strings without whitespace"
            )
        capabilities.append(capability)
    if len(capabilities) != len(set(capabilities)):
        raise ValueError("capabilities must be unique")
    return tuple(sorted(capabilities))


def _normalized_config_schema(value: object) -> dict[str, Any]:
    """Normalize a schema bounded to 64 levels and 10,000 JSON nodes."""

    if not isinstance(value, Mapping):
        raise ValueError("configSchema must be a mapping")
    pending: list[tuple[object, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()
    node_count = 0
    while pending:
        candidate, depth, leaving = pending.pop()
        if leaving:
            active_containers.remove(id(candidate))
            continue
        node_count += 1
        if node_count > _MAX_CONFIG_SCHEMA_NODES:
            raise ValueError(
                "configSchema must not contain more than "
                f"{_MAX_CONFIG_SCHEMA_NODES} JSON nodes"
            )
        if depth > _MAX_CONFIG_SCHEMA_DEPTH:
            raise ValueError(
                "configSchema nesting must not exceed "
                f"{_MAX_CONFIG_SCHEMA_DEPTH} levels"
            )
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in active_containers:
                raise ValueError("configSchema must not contain recursive values")
            active_containers.add(identity)
            pending.append((candidate, depth, True))
            pending.extend(
                (nested, depth + 1, False) for nested in candidate.values()
            )
        elif isinstance(candidate, (list, tuple)):
            identity = id(candidate)
            if identity in active_containers:
                raise ValueError("configSchema must not contain recursive values")
            active_containers.add(identity)
            pending.append((candidate, depth, True))
            pending.extend((nested, depth + 1, False) for nested in candidate)
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("configSchema must contain only finite JSON values") from error
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as error:
        raise ValueError(
            f"configSchema is not valid JSON Schema Draft 2020-12: {error.message}"
        ) from error
    pending: list[object] = [normalized]
    while pending:
        candidate = pending.pop()
        if isinstance(candidate, dict):
            for keyword in ("$ref", "$dynamicRef"):
                reference = candidate.get(keyword)
                if isinstance(reference, str) and not reference.startswith("#"):
                    raise ValueError(
                        f"configSchema {keyword} references must be local fragments"
                    )
            pending.extend(candidate.values())
        elif isinstance(candidate, list):
            pending.extend(candidate)
    return normalized


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _validate_type_ref(type_ref: object, *, nesting_depth: int = 0) -> str:
    if not isinstance(type_ref, str):
        raise ValueError("type reference must be a string")
    if not type_ref or type_ref != type_ref.strip() or any(character.isspace() for character in type_ref):
        raise ValueError("type reference must be non-empty and contain no whitespace")
    if type_ref in _PRIMITIVE_TYPE_REFS:
        return type_ref
    if "<" not in type_ref and ">" not in type_ref:
        try:
            SchemaId.parse(type_ref)
        except SchemaIdError as error:
            raise ValueError(str(error)) from error
        return type_ref
    opening = type_ref.find("<")
    if opening < 1 or not type_ref.endswith(">"):
        raise ValueError(f"invalid type reference {type_ref!r}")
    constructor = type_ref[:opening]
    if constructor not in _TYPE_CONSTRUCTOR_ARITY:
        raise ValueError(f"unsupported type constructor {constructor!r}")
    if nesting_depth >= _MAX_TYPE_REF_DEPTH:
        raise ValueError(
            "type reference nesting must not exceed "
            f"{_MAX_TYPE_REF_DEPTH} constructor levels"
        )
    body = type_ref[opening + 1 : -1]
    arguments: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(body):
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
            if depth < 0:
                raise ValueError(f"invalid type reference {type_ref!r}")
        elif character == "," and depth == 0:
            arguments.append(body[start:index])
            start = index + 1
    if depth != 0:
        raise ValueError(f"invalid type reference {type_ref!r}")
    arguments.append(body[start:])
    if len(arguments) != _TYPE_CONSTRUCTOR_ARITY[constructor] or any(not argument for argument in arguments):
        raise ValueError(f"invalid type reference {type_ref!r}")
    for argument in arguments:
        _validate_type_ref(argument, nesting_depth=nesting_depth + 1)
    return type_ref


def _validate_resource_type_ref(type_ref: object) -> str:
    if not isinstance(type_ref, str):
        raise ValueError("resource type reference must be a string")
    if not type_ref or type_ref != type_ref.strip() or any(character.isspace() for character in type_ref):
        raise ValueError("resource type reference must be non-empty and contain no whitespace")
    if "@" in type_ref or "/" in type_ref:
        try:
            SchemaId.parse(type_ref)
        except SchemaIdError as error:
            raise ValueError(str(error)) from error
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+", type_ref) is None:
        raise ValueError(
            "opaque resource type reference must use dot-separated identifier segments"
        )
    return type_ref


def _descriptor_bool(
    owner: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = owner.get(field_name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _validate_descriptor_name(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty string without whitespace")
    return value


def _validate_endpoint_name(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _ENDPOINT_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must match ^[A-Za-z][A-Za-z0-9_-]*$"
        )
    return value


def _parse_block_version(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("block descriptor version must be a positive integer")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("block descriptor version must be a positive integer")
        return value
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdecimal():
            raise ValueError("block descriptor version must be a positive integer")
        if value != "0" and value.startswith("0"):
            raise ValueError("block descriptor version must not use leading zeroes")
        parsed = int(value)
        if parsed < 1:
            raise ValueError("block descriptor version must be a positive integer")
        return parsed
    raise ValueError("block descriptor version must be a positive integer")


def _validate_json_pointer(pointer: object) -> str:
    if not isinstance(pointer, str):
        raise ValueError("configEquals.pointer must be a string")
    if pointer and not pointer.startswith("/"):
        raise ValueError("configEquals.pointer must be empty or start with '/'")
    if len(pointer) > 512:
        raise ValueError("configEquals.pointer must contain at most 512 characters")
    for token in pointer.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] != "~":
                index += 1
                continue
            if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                raise ValueError("configEquals.pointer contains an invalid JSON Pointer escape")
            index += 2
    return pointer


def _canonical_json(value: object) -> str:
    def validate_json_value(item: object) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if math.isfinite(item):
                return
            raise ValueError("configEquals.value must be a finite JSON value")
        if isinstance(item, list):
            for child in item:
                validate_json_value(child)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                validate_json_value(child)
            return
        raise ValueError("configEquals.value must be a finite JSON value")

    validate_json_value(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("configEquals.value must be a finite JSON value") from error


@dataclass(frozen=True, slots=True)
class OutputRequirednessPredicate:
    """Validated descriptor predicate for promoting an optional output to required."""

    operator: str
    pointer: str | None = None
    expected_json: str | None = None
    phase: str | None = None
    operands: tuple[OutputRequirednessPredicate, ...] = ()

    def __post_init__(self) -> None:
        if self.operator not in _OUTPUT_REQUIREDNESS_OPERATORS:
            raise ValueError(f"requiredWhen uses unsupported operator {self.operator!r}")
        if self.operator == "configEquals":
            if self.pointer is None or self.expected_json is None:
                raise ValueError("configEquals requires pointer and expected_json")
            _validate_json_pointer(self.pointer)
            try:
                expected = json.loads(self.expected_json)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("configEquals expected_json must be canonical JSON") from error
            if _canonical_json(expected) != self.expected_json:
                raise ValueError("configEquals expected_json must be canonical JSON")
            if self.phase is not None or self.operands:
                raise ValueError("configEquals must not declare phase or operands")
        elif self.operator == "phase":
            if self.phase not in _OUTPUT_REQUIREDNESS_PHASES:
                raise ValueError("phase must be initial or resumed")
            if self.pointer is not None or self.expected_json is not None or self.operands:
                raise ValueError("phase must not declare pointer, expected_json, or operands")
        else:
            if self.pointer is not None or self.expected_json is not None or self.phase is not None:
                raise ValueError(
                    f"{self.operator} must not declare pointer, expected_json, or phase"
                )
            if self.operator in {"all", "any"} and not self.operands:
                raise ValueError(f"{self.operator} must contain at least one predicate")
            if self.operator == "not" and len(self.operands) != 1:
                raise ValueError("not must contain exactly one predicate")
            if len(self.operands) > _MAX_OUTPUT_REQUIREDNESS_OPERANDS:
                raise ValueError(
                    f"{self.operator} must contain at most "
                    f"{_MAX_OUTPUT_REQUIREDNESS_OPERANDS} predicates"
                )
            if not all(
                isinstance(operand, OutputRequirednessPredicate)
                for operand in self.operands
            ):
                raise TypeError("requiredWhen operands must be predicates")

        pending = [(self, 0)]
        while pending:
            predicate, depth = pending.pop()
            if depth >= _MAX_OUTPUT_REQUIREDNESS_DEPTH:
                raise ValueError(
                    f"requiredWhen nesting must not exceed "
                    f"{_MAX_OUTPUT_REQUIREDNESS_DEPTH} levels"
                )
            pending.extend((operand, depth + 1) for operand in predicate.operands)


def parse_output_requiredness_predicate(
    value: object,
) -> OutputRequirednessPredicate:
    """Parse the closed, deterministic ``requiredWhen`` predicate language."""

    return _parse_output_requiredness_predicate(value, depth=0)


def _parse_output_requiredness_predicate(
    value: object,
    *,
    depth: int,
) -> OutputRequirednessPredicate:
    if depth >= _MAX_OUTPUT_REQUIREDNESS_DEPTH:
        raise ValueError(
            f"requiredWhen nesting must not exceed {_MAX_OUTPUT_REQUIREDNESS_DEPTH} levels"
        )
    if not isinstance(value, dict):
        raise ValueError("requiredWhen must be a mapping")
    if len(value) != 1:
        raise ValueError("requiredWhen must contain exactly one predicate operator")
    operator = next(iter(value), None)
    if operator not in _OUTPUT_REQUIREDNESS_OPERATORS:
        raise ValueError(f"requiredWhen uses unsupported operator {operator!r}")
    operand = value[operator]

    if operator == "configEquals":
        if not isinstance(operand, dict) or set(operand) != {"pointer", "value"}:
            raise ValueError("configEquals must contain exactly pointer and value")
        pointer = _validate_json_pointer(operand["pointer"])
        expected_json = _canonical_json(operand["value"])
        return OutputRequirednessPredicate(
            operator="configEquals",
            pointer=pointer,
            expected_json=expected_json,
        )

    if operator == "phase":
        if operand not in _OUTPUT_REQUIREDNESS_PHASES:
            raise ValueError("phase must be initial or resumed")
        return OutputRequirednessPredicate(operator="phase", phase=str(operand))

    if operator in {"all", "any"}:
        if not isinstance(operand, list) or not operand:
            raise ValueError(f"{operator} must be a non-empty list")
        if len(operand) > _MAX_OUTPUT_REQUIREDNESS_OPERANDS:
            raise ValueError(
                f"{operator} must contain at most {_MAX_OUTPUT_REQUIREDNESS_OPERANDS} predicates"
            )
        return OutputRequirednessPredicate(
            operator=operator,
            operands=tuple(
                _parse_output_requiredness_predicate(item, depth=depth + 1)
                for item in operand
            ),
        )

    return OutputRequirednessPredicate(
        operator="not",
        operands=(_parse_output_requiredness_predicate(operand, depth=depth + 1),),
    )


def _resolve_json_pointer(document: object, pointer: str) -> tuple[bool, object]:
    current = document
    if not pointer:
        return True, current
    for encoded_token in pointer.split("/")[1:]:
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return False, None
            current = current[token]
            continue
        if isinstance(current, list):
            if (
                not token.isascii()
                or not token.isdecimal()
                or (len(token) > 1 and token.startswith("0"))
            ):
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def evaluate_output_requiredness(
    predicate: OutputRequirednessPredicate,
    config: Mapping[str, object],
    *,
    phase: str,
) -> bool:
    """Evaluate a validated output predicate against immutable node inputs."""

    if not isinstance(predicate, OutputRequirednessPredicate):
        raise TypeError("predicate must be an OutputRequirednessPredicate")
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")
    if phase not in _OUTPUT_REQUIREDNESS_PHASES:
        raise ValueError("phase must be initial or resumed")
    if predicate.operator == "configEquals":
        if predicate.pointer is None:
            raise ValueError("configEquals predicate is missing its pointer")
        found, actual = _resolve_json_pointer(config, predicate.pointer)
        return found and _canonical_json(actual) == predicate.expected_json
    if predicate.operator == "phase":
        return predicate.phase == phase
    if predicate.operator == "all":
        return all(
            evaluate_output_requiredness(operand, config, phase=phase)
            for operand in predicate.operands
        )
    if predicate.operator == "any":
        return any(
            evaluate_output_requiredness(operand, config, phase=phase)
            for operand in predicate.operands
        )
    if predicate.operator != "not" or len(predicate.operands) != 1:
        raise ValueError("requiredWhen predicate is invalid")
    return not evaluate_output_requiredness(predicate.operands[0], config, phase=phase)


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    maturity: str
    capabilities: tuple[str, ...]
    blocks: tuple[dict[str, Any], ...]
    connector_factories: tuple[dict[str, Any], ...]
    adapters: tuple[dict[str, Any], ...]
    source: str
    raw: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "pluginId": self.plugin_id,
            "version": self.version,
            "maturity": self.maturity,
            "capabilities": list(self.capabilities),
            "blocks": len(self.blocks),
            "connectorFactories": len(self.connector_factories),
            "adapters": len(self.adapters),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class PortDescriptor:
    name: str
    type_ref: str | None = None
    required: bool = True
    required_when: OutputRequirednessPredicate | None = None

    def __post_init__(self) -> None:
        _validate_endpoint_name(self.name, field_name="port name")
        if self.type_ref is not None:
            _validate_type_ref(self.type_ref)
        if not isinstance(self.required, bool):
            raise TypeError("port required must be a boolean")
        if self.required_when is not None and not isinstance(
            self.required_when, OutputRequirednessPredicate
        ):
            raise TypeError("port required_when must be an OutputRequirednessPredicate")

    def required_for(self, config: Mapping[str, object], *, phase: str) -> bool:
        return self.required or (
            self.required_when is not None
            and evaluate_output_requiredness(self.required_when, config, phase=phase)
        )


@dataclass(frozen=True, slots=True)
class ResourceSlotDescriptor:
    name: str
    type_ref: str | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        _validate_descriptor_name(self.name, field_name="resource slot name")
        if self.type_ref is not None:
            _validate_resource_type_ref(self.type_ref)
        if not isinstance(self.optional, bool):
            raise TypeError("resource slot optional must be a boolean")


@dataclass(frozen=True, slots=True)
class BlockDescriptor:
    type_id: str
    version: int
    inputs: tuple[PortDescriptor, ...] = ()
    outputs: tuple[PortDescriptor, ...] = ()
    resource_slots: tuple[ResourceSlotDescriptor, ...] = ()
    capabilities: tuple[str, ...] = ()
    config_schema: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object"},
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        type_id = _validate_descriptor_name(self.type_id, field_name="block type_id")
        if "@" in type_id:
            raise ValueError("block type_id must not include a version suffix")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("block version must be a positive integer")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        resource_slots = tuple(self.resource_slots)
        if not all(isinstance(port, PortDescriptor) for port in inputs):
            raise TypeError("block inputs must contain only PortDescriptor values")
        if not all(isinstance(port, PortDescriptor) for port in outputs):
            raise TypeError("block outputs must contain only PortDescriptor values")
        if not all(
            isinstance(slot, ResourceSlotDescriptor) for slot in resource_slots
        ):
            raise TypeError(
                "block resource_slots must contain only ResourceSlotDescriptor values"
            )
        if any(port.required_when is not None for port in inputs):
            raise ValueError("block inputs must not declare required_when")
        for field_name, descriptors in (
            ("input", inputs),
            ("output", outputs),
            ("resource slot", resource_slots),
        ):
            names = [descriptor.name for descriptor in descriptors]
            if len(names) != len(set(names)):
                raise ValueError(f"block {field_name} names must be unique")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "resource_slots", resource_slots)
        object.__setattr__(self, "capabilities", _block_capabilities(self.capabilities))
        object.__setattr__(
            self,
            "config_schema",
            _freeze_json(_normalized_config_schema(self.config_schema)),
        )

    @property
    def block_id(self) -> str:
        return f"{self.type_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class BlockCatalog:
    descriptors: Mapping[str, BlockDescriptor]
    allow_unknown_blocks: bool = False

    def __post_init__(self) -> None:
        descriptors = dict(self.descriptors)
        for block_id, descriptor in descriptors.items():
            if not isinstance(descriptor, BlockDescriptor):
                raise TypeError(
                    f"block catalog descriptor {block_id!r} must be a BlockDescriptor"
                )
            if block_id != descriptor.block_id:
                raise ValueError(
                    f"block catalog key {block_id!r} does not match descriptor "
                    f"{descriptor.block_id!r}"
                )
        object.__setattr__(self, "descriptors", MappingProxyType(descriptors))
        if not isinstance(self.allow_unknown_blocks, bool):
            raise TypeError("allow_unknown_blocks must be a boolean")

    @classmethod
    def from_blocks(
        cls,
        blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        allow_unknown_blocks: bool = False,
    ) -> BlockCatalog:
        descriptors: dict[str, BlockDescriptor] = {}
        for block_index, block in enumerate(blocks):
            block_type = block.get("typeId") or block.get("type_id") or block.get("block")
            version = block.get("version")
            if isinstance(block_type, str) and "@" in block_type and version is None:
                block_type, version = block_type.rsplit("@", 1)
            if not isinstance(block_type, str) or not block_type or version is None:
                raise ValueError(f"block catalog entry {block_index} requires typeId and version")
            try:
                parsed_version = _parse_block_version(version)
            except ValueError as error:
                raise ValueError(f"block catalog entry {block_index} version is invalid: {error}") from error
            try:
                capabilities = _block_capabilities(block.get("capabilities", []))
            except ValueError as error:
                raise ValueError(
                    f"block catalog entry {block_index} capabilities are invalid: {error}"
                ) from error
            try:
                config_schema = _normalized_config_schema(
                    block.get("configSchema", {"type": "object"})
                )
            except ValueError as error:
                raise ValueError(
                    f"block catalog entry {block_index} configSchema is invalid: {error}"
                ) from error
            inputs: list[PortDescriptor] = []
            input_names: set[str] = set()
            raw_inputs = block.get("inputs", [])
            if not isinstance(raw_inputs, list):
                raise ValueError(f"block catalog entry {block_index} inputs must be a list")
            for port_index, port in enumerate(raw_inputs):
                if not isinstance(port, dict) or not isinstance(port.get("name"), str) or not port["name"]:
                    raise ValueError(
                        f"block catalog entry {block_index} input {port_index} requires a non-empty name"
                    )
                if port["name"] in input_names:
                    raise ValueError(
                        f"block catalog entry {block_index} has duplicate input {port['name']!r}"
                    )
                input_names.add(port["name"])
                if "requiredWhen" in port:
                    raise ValueError(
                        f"block catalog entry {block_index} input {port['name']} "
                        "must not declare requiredWhen"
                    )
                type_ref = port.get("type")
                if type_ref is not None:
                    try:
                        type_ref = _validate_type_ref(type_ref)
                    except ValueError as error:
                        raise ValueError(
                            f"block catalog entry {block_index} input {port['name']} "
                            f"has invalid type {type_ref}: {error}"
                        ) from error
                inputs.append(
                    PortDescriptor(
                        name=port["name"],
                        type_ref=type_ref,
                        required=_descriptor_bool(port, "required", default=True),
                    )
                )
            outputs: list[PortDescriptor] = []
            output_names: set[str] = set()
            raw_outputs = block.get("outputs", [])
            if not isinstance(raw_outputs, list):
                raise ValueError(f"block catalog entry {block_index} outputs must be a list")
            for port_index, port in enumerate(raw_outputs):
                if not isinstance(port, dict) or not isinstance(port.get("name"), str) or not port["name"]:
                    raise ValueError(
                        f"block catalog entry {block_index} output {port_index} requires a non-empty name"
                    )
                if port["name"] in output_names:
                    raise ValueError(
                        f"block catalog entry {block_index} has duplicate output {port['name']!r}"
                    )
                output_names.add(port["name"])
                type_ref = port.get("type")
                if type_ref is not None:
                    try:
                        type_ref = _validate_type_ref(type_ref)
                    except ValueError as error:
                        raise ValueError(
                            f"block catalog entry {block_index} output {port['name']} "
                            f"has invalid type {type_ref}: {error}"
                        ) from error
                required_when = None
                if "requiredWhen" in port:
                    try:
                        required_when = parse_output_requiredness_predicate(
                            port["requiredWhen"]
                        )
                    except ValueError as error:
                        raise ValueError(
                            f"block catalog entry {block_index} output {port['name']} "
                            f"has invalid requiredWhen: {error}"
                        ) from error
                outputs.append(
                    PortDescriptor(
                        name=port["name"],
                        type_ref=type_ref,
                        required=_descriptor_bool(port, "required", default=True),
                        required_when=required_when,
                    )
                )
            resource_slots: list[ResourceSlotDescriptor] = []
            resource_slot_names: set[str] = set()
            raw_slots = block.get("resourceSlots", [])
            if isinstance(raw_slots, dict):
                normalized_slots: list[dict[str, Any]] = []
                for name, slot in raw_slots.items():
                    if not isinstance(slot, dict):
                        raise ValueError(
                            f"block catalog entry {block_index} resource slot {name!r} must be a mapping"
                        )
                    if "name" in slot:
                        raise ValueError(
                            f"block catalog entry {block_index} mapping resource slot "
                            f"{name!r} must not declare name; its mapping key defines the name"
                        )
                    normalized_slots.append({**slot, "name": name})
                raw_slots = normalized_slots
            elif not isinstance(raw_slots, list):
                raise ValueError(
                    f"block catalog entry {block_index} resourceSlots must be a list or mapping"
                )
            for slot_index, slot in enumerate(raw_slots):
                if not isinstance(slot, dict) or not isinstance(slot.get("name"), str) or not slot["name"]:
                    raise ValueError(
                        f"block catalog entry {block_index} resource slot {slot_index} requires a non-empty name"
                    )
                if slot["name"] in resource_slot_names:
                    raise ValueError(
                        f"block catalog entry {block_index} has duplicate resource slot {slot['name']!r}"
                    )
                resource_slot_names.add(slot["name"])
                type_ref = slot.get("type")
                if type_ref is not None:
                    try:
                        type_ref = _validate_resource_type_ref(type_ref)
                    except ValueError as error:
                        raise ValueError(
                            f"block catalog entry {block_index} resource slot {slot['name']} "
                            f"has invalid type {type_ref}: {error}"
                        ) from error
                resource_slots.append(
                    ResourceSlotDescriptor(
                        name=slot["name"],
                        type_ref=type_ref,
                        optional=_descriptor_bool(slot, "optional", default=False),
                    )
                )
            descriptor = BlockDescriptor(
                type_id=str(block_type),
                version=parsed_version,
                inputs=tuple(inputs),
                outputs=tuple(outputs),
                resource_slots=tuple(resource_slots),
                capabilities=capabilities,
                config_schema=config_schema,
            )
            if descriptor.block_id in descriptors:
                raise ValueError(f"duplicate block catalog descriptor {descriptor.block_id}")
            descriptors[descriptor.block_id] = descriptor
        return cls(descriptors, allow_unknown_blocks=allow_unknown_blocks)

    @classmethod
    def from_manifests(
        cls,
        manifests: tuple[PluginManifest, ...] | list[PluginManifest],
        *,
        allow_unknown_blocks: bool = False,
    ) -> BlockCatalog:
        blocks: list[dict[str, Any]] = []
        for manifest in manifests:
            blocks.extend(manifest.blocks)
        return cls.from_blocks(blocks, allow_unknown_blocks=allow_unknown_blocks)

    def get(self, block_id: str) -> BlockDescriptor | None:
        return self.descriptors.get(block_id)


@dataclass(frozen=True, slots=True)
class PluginRegistry:
    manifests: tuple[PluginManifest, ...]
    diagnostics: DiagnosticSet

    @property
    def ok(self) -> bool:
        return self.diagnostics.ok

    def summaries(self) -> list[dict[str, Any]]:
        return [manifest.summary() for manifest in self.manifests]


def load_plugin_manifest(path: str | Path) -> PluginManifest:
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        if source.suffix == ".json":
            document = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
        else:
            document = yaml.load(stream, Loader=_DuplicateKeySafeLoader)
    diagnostics = validate_plugin_manifest(document)
    if not diagnostics.ok:
        messages = "; ".join(f"{item.code} {item.path}: {item.message}" for item in diagnostics.diagnostics)
        raise ValueError(f"{source}: invalid plugin manifest: {messages}")
    return plugin_manifest_from_document(document, str(source))


def plugin_manifest_from_document(document: dict[str, Any], source: str = "<memory>") -> PluginManifest:
    diagnostics = validate_plugin_manifest(document)
    if not diagnostics.ok:
        messages = "; ".join(
            f"{item.code} {item.path}: {item.message}"
            for item in diagnostics.diagnostics
        )
        raise ValueError(f"{source}: invalid plugin manifest: {messages}")
    document = migrate_document(document)
    spec = document.get("spec", {})
    metadata = document.get("metadata", {})
    return PluginManifest(
        plugin_id=str(spec.get("pluginId") or metadata.get("name")),
        version=str(spec.get("version") or metadata.get("version") or "0.0.0"),
        maturity=str(spec.get("maturity") or "experimental"),
        capabilities=tuple(sorted(str(item) for item in spec.get("capabilities", []))),
        blocks=tuple(item for item in spec.get("blocks", []) if isinstance(item, dict)),
        connector_factories=tuple(item for item in spec.get("connectorFactories", []) if isinstance(item, dict)),
        adapters=tuple(item for item in spec.get("adapters", []) if isinstance(item, dict)),
        source=source,
        raw=document,
    )


def validate_plugin_manifest(document: Any) -> DiagnosticSet:
    diagnostics: list[Diagnostic] = []
    schema_violations = ()
    if not isinstance(document, dict):
        return DiagnosticSet((Diagnostic("GB2001", "plugin manifest must be a mapping"),))
    domain_violations = tuple(
        violation
        for violation in resource_schema_errors(document)
        if violation.keyword
        in {"finiteNumber", "jsonObjectKey", "jsonValue", "maxDepth", "recursive"}
    )
    if domain_violations:
        return DiagnosticSet(
            tuple(
                Diagnostic(violation.code, violation.message, violation.path)
                for violation in domain_violations
            )
        )
    try:
        document = _migrate_document_unchecked(document)
    except MigrationError as error:
        diagnostics.append(
            Diagnostic(
                error.code,
                error.message,
                error.path,
            )
        )
    else:
        schema_violations = resource_schema_errors(document)
    if document.get("kind") != "PluginManifest":
        diagnostics.append(Diagnostic("GB2003", "plugin manifest kind must be PluginManifest", "$.kind"))
    metadata = document.get("metadata")
    spec = document.get("spec")
    if not isinstance(metadata, dict):
        diagnostics.append(Diagnostic("GB2004", "metadata must be a mapping", "$.metadata"))
        metadata = {}
    if not isinstance(spec, dict):
        diagnostics.append(Diagnostic("GB2005", "spec must be a mapping", "$.spec"))
        spec = {}
    plugin_id = spec.get("pluginId") or metadata.get("name")
    if not isinstance(plugin_id, str) or not plugin_id:
        diagnostics.append(Diagnostic("GB2006", "spec.pluginId or metadata.name is required", "$.spec.pluginId"))
    blocks = spec.get("blocks", [])
    if not isinstance(blocks, list):
        diagnostics.append(Diagnostic("GB2007", "spec.blocks must be a list", "$.spec.blocks"))
        blocks = []
    seen_blocks: set[tuple[str, str]] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            diagnostics.append(Diagnostic("GB2008", "block descriptor must be a mapping", f"$.spec.blocks[{index}]"))
            continue
        block_type = block.get("typeId") or block.get("type_id") or block.get("block")
        version = block.get("version")
        version_path = f"$.spec.blocks[{index}].version"
        if isinstance(block_type, str) and "@" in block_type and version is None:
            block_type, version = block_type.rsplit("@", 1)
            version_path = f"$.spec.blocks[{index}].typeId"
        if not isinstance(block_type, str) or not block_type:
            diagnostics.append(Diagnostic("GB2009", "block descriptor requires typeId", f"$.spec.blocks[{index}].typeId"))
        parsed_version: int | None = None
        if version is None:
            diagnostics.append(Diagnostic("GB2010", "block descriptor requires version", f"$.spec.blocks[{index}].version"))
        else:
            try:
                parsed_version = _parse_block_version(version)
            except ValueError as error:
                diagnostics.append(
                    Diagnostic(
                        "GB2016",
                        f"block descriptor version is invalid: {error}",
                        version_path,
                    )
                )
        raw_capabilities = block.get("capabilities", _MISSING)
        capabilities_path = f"$.spec.blocks[{index}].capabilities"
        if raw_capabilities is _MISSING:
            diagnostics.append(
                Diagnostic(
                    "GB2018",
                    "block descriptor capabilities are required",
                    capabilities_path,
                )
            )
        else:
            try:
                _block_capabilities(raw_capabilities)
            except ValueError as error:
                diagnostics.append(
                    Diagnostic(
                        "GB2018",
                        f"block descriptor capabilities are invalid: {error}",
                        capabilities_path,
                    )
                )
        raw_config_schema = block.get("configSchema", _MISSING)
        config_schema_path = f"$.spec.blocks[{index}].configSchema"
        if raw_config_schema is _MISSING:
            diagnostics.append(
                Diagnostic(
                    "GB2018",
                    "block descriptor configSchema is required",
                    config_schema_path,
                )
            )
        else:
            try:
                _normalized_config_schema(raw_config_schema)
            except ValueError as error:
                diagnostics.append(
                    Diagnostic(
                        "GB2018",
                        f"block descriptor configSchema is invalid: {error}",
                        config_schema_path,
                    )
                )
        for direction in ("inputs", "outputs"):
            ports = block.get(direction, [])
            if not isinstance(ports, list):
                diagnostics.append(Diagnostic("GB2015", f"block {direction} must be a list", f"$.spec.blocks[{index}].{direction}"))
                continue
            seen_port_names: set[str] = set()
            for port_index, port in enumerate(ports):
                if not isinstance(port, dict) or not isinstance(port.get("name"), str) or not port["name"]:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block {direction} entries require a non-empty name",
                            f"$.spec.blocks[{index}].{direction}[{port_index}].name",
                        )
                    )
                    continue
                try:
                    _validate_endpoint_name(
                        port["name"], field_name=f"block {direction[:-1]} name"
                    )
                except ValueError as error:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block {direction[:-1]} name is invalid: {error}",
                            f"$.spec.blocks[{index}].{direction}[{port_index}].name",
                        )
                    )
                if port["name"] in seen_port_names:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block {direction} names must be unique",
                            f"$.spec.blocks[{index}].{direction}[{port_index}].name",
                        )
                    )
                seen_port_names.add(port["name"])
                try:
                    _descriptor_bool(port, "required", default=True)
                except ValueError as error:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block {direction[:-1]} required flag is invalid: {error}",
                            f"$.spec.blocks[{index}].{direction}[{port_index}].required",
                        )
                    )
                required_when_path = (
                    f"$.spec.blocks[{index}].{direction}[{port_index}].requiredWhen"
                )
                if direction == "inputs" and "requiredWhen" in port:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            "requiredWhen is valid only on output ports",
                            required_when_path,
                        )
                    )
                elif direction == "outputs" and "requiredWhen" in port:
                    try:
                        parse_output_requiredness_predicate(port["requiredWhen"])
                    except ValueError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB2015",
                                f"block output requiredWhen is invalid: {error}",
                                required_when_path,
                            )
                        )
                type_ref = port.get("type")
                if type_ref is not None:
                    try:
                        _validate_type_ref(type_ref)
                    except ValueError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"block {direction[:-1]} type schema id is invalid: {error}",
                                f"$.spec.blocks[{index}].{direction}[{port_index}].type",
                            )
                        )
        resource_slots = block.get("resourceSlots", [])
        if isinstance(resource_slots, list):
            for slot_index, slot in enumerate(resource_slots):
                if not isinstance(slot, dict) or not isinstance(slot.get("name"), str) or not slot["name"]:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            "block resource slot entries require a non-empty name",
                            f"$.spec.blocks[{index}].resourceSlots[{slot_index}].name",
                        )
                    )
                    continue
                try:
                    _descriptor_bool(slot, "optional", default=False)
                except ValueError as error:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block resource slot optional flag is invalid: {error}",
                            f"$.spec.blocks[{index}].resourceSlots[{slot_index}].optional",
                        )
                    )
                if (
                    slot.get("type") is not None
                ):
                    try:
                        _validate_resource_type_ref(slot["type"])
                    except ValueError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"resource slot type schema id is invalid: {error}",
                                f"$.spec.blocks[{index}].resourceSlots[{slot_index}].type",
                            )
                        )
        elif isinstance(resource_slots, dict):
            for slot_name, slot in resource_slots.items():
                if not isinstance(slot_name, str) or not slot_name or not isinstance(slot, dict):
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            "block resource slot entries must be named mappings",
                            f"$.spec.blocks[{index}].resourceSlots.{slot_name}",
                        )
                    )
                    continue
                try:
                    _descriptor_bool(slot, "optional", default=False)
                except ValueError as error:
                    diagnostics.append(
                        Diagnostic(
                            "GB2015",
                            f"block resource slot optional flag is invalid: {error}",
                            f"$.spec.blocks[{index}].resourceSlots.{slot_name}.optional",
                        )
                    )
                if (
                    slot.get("type") is not None
                ):
                    try:
                        _validate_resource_type_ref(slot["type"])
                    except ValueError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"resource slot type schema id is invalid: {error}",
                                f"$.spec.blocks[{index}].resourceSlots.{slot_name}.type",
                            )
                        )
        else:
            diagnostics.append(
                Diagnostic(
                    "GB2015",
                    "block resourceSlots must be a list or mapping",
                    f"$.spec.blocks[{index}].resourceSlots",
                )
            )
        key = (
            str(block_type),
            str(parsed_version) if parsed_version is not None else str(version),
        )
        if key in seen_blocks:
            diagnostics.append(
                Diagnostic("GB2011", "duplicate block descriptor in plugin manifest", f"$.spec.blocks[{index}]")
            )
        seen_blocks.add(key)
    semantic_paths = {diagnostic.path for diagnostic in diagnostics}
    if any(diagnostic.code == "GB2006" for diagnostic in diagnostics):
        # GB2006 covers either accepted source of plugin identity.
        semantic_paths.add("$.metadata.name")
    seen_diagnostics = set(diagnostics)
    for violation in schema_violations:
        if violation.path in semantic_paths:
            continue
        if violation.keyword in {"anyOf", "oneOf"} and any(
            semantic_path.startswith(f"{violation.path}.")
            or semantic_path.startswith(f"{violation.path}[")
            for semantic_path in semantic_paths
        ):
            # Composite schemas report their parent when a selected branch has
            # already produced a more precise semantic diagnostic below it.
            continue
        covered_paths: set[str] = set()
        if violation.keyword == "additionalProperties":
            prefix = "unexpected properties are not allowed: "
            try:
                unexpected = json.loads(violation.message.removeprefix(prefix))
            except json.JSONDecodeError:
                unexpected = []
            covered_paths = {
                f"{violation.path}.{key}"
                for key in unexpected
                if isinstance(key, str) and key.isidentifier()
            }
        elif violation.keyword == "required":
            missing_property = re.fullmatch(
                r"'([^']+)' is a required property",
                violation.message,
            )
            if (
                missing_property is not None
                and missing_property.group(1).isidentifier()
            ):
                covered_paths = {f"{violation.path}.{missing_property.group(1)}"}
        if covered_paths and covered_paths.issubset(semantic_paths):
            continue
        diagnostic = Diagnostic(violation.code, violation.message, violation.path)
        if diagnostic in seen_diagnostics:
            continue
        diagnostics.append(diagnostic)
        seen_diagnostics.add(diagnostic)
    return DiagnosticSet(tuple(diagnostics))


def discover_plugins(paths: list[str | Path] | None = None, include_installed: bool = True) -> PluginRegistry:
    diagnostics: list[Diagnostic] = []
    manifests: list[PluginManifest] = []
    with resources.files("graphblocks").joinpath("data/builtin-plugin.yaml").open("r", encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    builtin_diagnostics = validate_plugin_manifest(document)
    diagnostics.extend(builtin_diagnostics.diagnostics)
    manifests.append(plugin_manifest_from_document(document, "graphblocks:data/builtin-plugin.yaml"))

    for search_path in paths or []:
        candidate = Path(search_path)
        files = [candidate]
        if candidate.is_dir():
            files = sorted(
                path
                for path in candidate.rglob("*")
                if path.is_file() and path.suffix in {".yaml", ".yml", ".json"}
            )
        for file_path in files:
            try:
                manifests.append(load_plugin_manifest(file_path))
            except Exception as exc:
                diagnostics.append(Diagnostic("GB2012", str(exc), str(file_path)))

    if include_installed:
        for distribution in importlib.metadata.distributions():
            for file in distribution.files or []:
                relative = file.as_posix()
                if relative in STATIC_MANIFEST_NAMES:
                    located = Path(distribution.locate_file(file))
                    try:
                        manifests.append(load_plugin_manifest(located))
                    except Exception as exc:
                        diagnostics.append(Diagnostic("GB2012", str(exc), str(located)))
        entry_points = importlib.metadata.entry_points()
        for entry_point in entry_points.select(group="graphblocks.manifests"):
            located = Path(entry_point.dist.locate_file(entry_point.value))
            try:
                manifests.append(load_plugin_manifest(located))
            except Exception as exc:
                diagnostics.append(Diagnostic("GB2012", str(exc), str(located)))

    seen_plugins: dict[tuple[str, str], PluginManifest] = {}
    seen_implementations: dict[tuple[str, str, str], PluginManifest] = {}
    unique: list[PluginManifest] = []
    for manifest in manifests:
        plugin_key = (manifest.plugin_id, manifest.version)
        if plugin_key in seen_plugins:
            diagnostics.append(
                Diagnostic(
                    "GB2013",
                    f"duplicate plugin id/version {manifest.plugin_id}@{manifest.version}",
                    manifest.source,
                )
            )
            continue
        seen_plugins[plugin_key] = manifest
        for block in manifest.blocks:
            block_type = block.get("typeId") or block.get("type_id") or block.get("block")
            version = block.get("version")
            if isinstance(block_type, str) and "@" in block_type and version is None:
                block_type, version = block_type.rsplit("@", 1)
            implementation = str(block.get("implementation") or block.get("implementationId") or "")
            if implementation:
                block_key = (str(block_type), str(version), implementation)
                if block_key in seen_implementations:
                    diagnostics.append(
                        Diagnostic(
                            "GB2014",
                            f"duplicate implementation {implementation!r} for {block_type}@{version}",
                            manifest.source,
                        )
                    )
                else:
                    seen_implementations[block_key] = manifest
        unique.append(manifest)
    unique.sort(key=lambda item: (item.plugin_id, item.version, item.source))
    return PluginRegistry(tuple(unique), DiagnosticSet(tuple(diagnostics)))


@lru_cache(maxsize=2)
def builtin_block_catalog(
    *,
    profile: Literal["preview", "stable"] = "preview",
) -> BlockCatalog:
    """Return an immutable built-in preview or stable block catalog."""

    if profile not in {"preview", "stable"}:
        raise ValueError("profile must be 'preview' or 'stable'")

    registry = discover_plugins(include_installed=False)
    if not registry.ok:
        messages = "; ".join(
            f"{item.code} {item.path}: {item.message}"
            for item in registry.diagnostics.diagnostics
        )
        raise RuntimeError(f"built-in plugin catalog is invalid: {messages}")
    catalog = BlockCatalog.from_manifests(registry.manifests)
    if profile == "preview":
        return catalog
    descriptors = {
        block_id: catalog.descriptors[block_id]
        for block_id in sorted(_STABLE_CORE_BLOCK_IDS)
    }
    descriptors["control.map@2"] = replace(
        descriptors["control.map@2"],
        config_schema=_STABLE_CONTROL_MAP_CONFIG_SCHEMA,
    )
    return BlockCatalog(descriptors)
