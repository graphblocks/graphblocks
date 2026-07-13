from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import yaml

from .diagnostics import Diagnostic, DiagnosticSet
from .schema import SchemaId, SchemaIdError

PLUGIN_API_VERSION = "graphblocks.ai/v1alpha1"
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


def _validate_type_ref(type_ref: object) -> str:
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
        _validate_type_ref(argument)
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


@dataclass(frozen=True, slots=True)
class ResourceSlotDescriptor:
    name: str
    type_ref: str | None = None
    optional: bool = False


@dataclass(frozen=True, slots=True)
class BlockDescriptor:
    type_id: str
    version: int
    inputs: tuple[PortDescriptor, ...] = ()
    outputs: tuple[PortDescriptor, ...] = ()
    resource_slots: tuple[ResourceSlotDescriptor, ...] = ()

    @property
    def block_id(self) -> str:
        return f"{self.type_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class BlockCatalog:
    descriptors: Mapping[str, BlockDescriptor]
    allow_unknown_blocks: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "descriptors", MappingProxyType(dict(self.descriptors)))
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
                outputs.append(
                    PortDescriptor(
                        name=port["name"],
                        type_ref=type_ref,
                        required=_descriptor_bool(port, "required", default=True),
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
                    normalized_slots.append({"name": name, **slot})
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
                str(block_type),
                parsed_version,
                tuple(inputs),
                tuple(outputs),
                tuple(resource_slots),
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
            document = json.load(stream)
        else:
            document = yaml.safe_load(stream)
    diagnostics = validate_plugin_manifest(document)
    if not diagnostics.ok:
        messages = "; ".join(f"{item.code} {item.path}: {item.message}" for item in diagnostics.diagnostics)
        raise ValueError(f"{source}: invalid plugin manifest: {messages}")
    return plugin_manifest_from_document(document, str(source))


def plugin_manifest_from_document(document: dict[str, Any], source: str = "<memory>") -> PluginManifest:
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
    if not isinstance(document, dict):
        return DiagnosticSet((Diagnostic("GB2001", "plugin manifest must be a mapping"),))
    if document.get("apiVersion") != PLUGIN_API_VERSION:
        diagnostics.append(Diagnostic("GB2002", "plugin manifest apiVersion must be graphblocks.ai/v1alpha1", "$.apiVersion"))
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
                type_ref = port.get("type")
                if type_ref is not None:
                    try:
                        _validate_type_ref(type_ref)
                    except ValueError as error:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidSchemaId",
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
                                "InvalidSchemaId",
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
                                "InvalidSchemaId",
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


@lru_cache(maxsize=1)
def builtin_block_catalog() -> BlockCatalog:
    """Return the immutable catalog shipped with the core package."""

    registry = discover_plugins(include_installed=False)
    if not registry.ok:
        messages = "; ".join(
            f"{item.code} {item.path}: {item.message}"
            for item in registry.diagnostics.diagnostics
        )
        raise RuntimeError(f"built-in plugin catalog is invalid: {messages}")
    return BlockCatalog.from_manifests(registry.manifests)
