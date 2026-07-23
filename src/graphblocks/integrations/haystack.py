from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from graphblocks import ContentPart, Message, canonical_dumps, canonical_loads
from graphblocks.documents import FrozenDict, _freeze_value


class HaystackBridgeError(ValueError):
    """Raised when a Haystack bridge contract is invalid."""


def _wire_string(field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise HaystackBridgeError(f"{field_name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise HaystackBridgeError(
            f"{field_name} must contain only Unicode scalar values"
        ) from error
    return value


def _stable_string(field_name: str, value: object) -> str:
    value = _wire_string(field_name, value)
    if not value.strip() or value != value.strip():
        raise HaystackBridgeError(f"{field_name} must be a stable non-empty string")
    return value


def _positive_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HaystackBridgeError(f"{field_name} must be a positive integer")
    return value


def _mapping_snapshot(field_name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HaystackBridgeError(f"{field_name} must be a mapping")
    try:
        snapshot = canonical_loads(canonical_dumps(value))
    except Exception as error:
        if "Unicode scalar" in str(error):
            raise HaystackBridgeError(
                f"{field_name} must contain only Unicode scalar values"
            ) from error
        raise HaystackBridgeError(
            f"{field_name} must contain strict JSON"
        ) from error
    if not isinstance(snapshot, dict):
        raise HaystackBridgeError(f"{field_name} must be a mapping")
    if field_name == "metadata" and "haystack" in snapshot:
        raise HaystackBridgeError("metadata must not override the reserved haystack field")
    return FrozenDict(
        {
            key: _freeze_value("Haystack bridge", item)
            for key, item in snapshot.items()
        }
    )


def _validate_block_type_id(block_type_id: object) -> str:
    normalized = _stable_string("block_type_id", block_type_id)
    if any(character.isspace() for character in normalized) or "@" in normalized:
        raise HaystackBridgeError(f"block_type_id is invalid: {block_type_id!r}")
    return normalized


def _port_descriptors(owner: str, ports: Mapping[str, str], *, require_output: bool = False) -> list[dict[str, object]]:
    if not isinstance(ports, Mapping):
        raise HaystackBridgeError(f"{owner} ports must be a mapping")
    if require_output and not ports:
        raise HaystackBridgeError(f"{owner} outputs must not be empty")
    normalized: list[tuple[str, str]] = []
    for name, type_ref in ports.items():
        normalized_name = _stable_string(f"{owner} port name", name)
        normalized_type = _stable_string(f"{owner} port {name!r} type", type_ref)
        normalized.append((normalized_name, normalized_type))
    descriptors = [
        {"name": name, "type": type_ref, "required": True}
        for name, type_ref in sorted(normalized)
    ]
    return descriptors


@dataclass(frozen=True, slots=True)
class HaystackComponentBlock:
    component_ref: str
    block_type_id: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    version: int = 1
    descriptor_source: Literal["explicit", "introspected"] = "explicit"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_string("component_ref", self.component_ref)
        _positive_integer("version", self.version)
        if self.descriptor_source not in {"explicit", "introspected"}:
            raise HaystackBridgeError("descriptor_source is invalid")
        object.__setattr__(self, "block_type_id", _validate_block_type_id(self.block_type_id))
        object.__setattr__(self, "inputs", _mapping_snapshot("inputs", self.inputs))
        object.__setattr__(self, "outputs", _mapping_snapshot("outputs", self.outputs))
        object.__setattr__(self, "metadata", _mapping_snapshot("metadata", self.metadata))
        _port_descriptors("component", self.inputs)
        _port_descriptors("component", self.outputs, require_output=True)

    def block_descriptor(self) -> dict[str, object]:
        return {
            "typeId": self.block_type_id,
            "version": self.version,
            "inputs": _port_descriptors("component", self.inputs),
            "outputs": _port_descriptors("component", self.outputs, require_output=True),
            "resourceSlots": [{"name": "component", "type": "haystack.component", "optional": False}],
            "metadata": {
                "haystack": {
                    "componentRef": self.component_ref,
                    "descriptorSource": self.descriptor_source,
                    "kind": "component",
                },
                **deepcopy(dict(self.metadata)),
            },
        }


@dataclass(frozen=True, slots=True)
class HaystackPipelineBlock:
    pipeline_ref: str
    block_type_id: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    version: int = 1
    async_pipeline: bool = False
    descriptor_source: Literal["explicit", "introspected"] = "explicit"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_string("pipeline_ref", self.pipeline_ref)
        _positive_integer("version", self.version)
        if not isinstance(self.async_pipeline, bool):
            raise HaystackBridgeError("async_pipeline must be a boolean")
        if self.descriptor_source not in {"explicit", "introspected"}:
            raise HaystackBridgeError("descriptor_source is invalid")
        object.__setattr__(self, "block_type_id", _validate_block_type_id(self.block_type_id))
        object.__setattr__(self, "inputs", _mapping_snapshot("inputs", self.inputs))
        object.__setattr__(self, "outputs", _mapping_snapshot("outputs", self.outputs))
        object.__setattr__(self, "metadata", _mapping_snapshot("metadata", self.metadata))
        _port_descriptors("pipeline", self.inputs)
        _port_descriptors("pipeline", self.outputs, require_output=True)

    def block_descriptor(self) -> dict[str, object]:
        return {
            "typeId": self.block_type_id,
            "version": self.version,
            "inputs": _port_descriptors("pipeline", self.inputs),
            "outputs": _port_descriptors("pipeline", self.outputs, require_output=True),
            "resourceSlots": [{"name": "pipeline", "type": "haystack.pipeline", "optional": False}],
            "metadata": {
                "haystack": {
                    "asyncPipeline": self.async_pipeline,
                    "descriptorSource": self.descriptor_source,
                    "kind": "pipeline",
                    "pipelineRef": self.pipeline_ref,
                },
                **deepcopy(dict(self.metadata)),
            },
        }


@dataclass(frozen=True, slots=True)
class HaystackDescriptorDiagnostic:
    subject_ref: str
    subject_kind: Literal["component", "pipeline"]
    reason: str

    def __post_init__(self) -> None:
        _stable_string("subject_ref", self.subject_ref)
        if self.subject_kind not in {"component", "pipeline"}:
            raise HaystackBridgeError("subject_kind is invalid")
        object.__setattr__(self, "reason", _stable_string("reason", self.reason))

    def diagnostic_contract(self) -> dict[str, object]:
        return {
            "code": "HaystackExplicitDescriptorRequired",
            "message": (
                f"{self.subject_kind} {self.subject_ref!r} requires an explicit GraphBlocks descriptor"
            ),
            "metadata": {
                "reason": self.reason,
                "subject_kind": self.subject_kind,
            },
        }


def explicit_descriptor_required(
    *,
    subject_ref: str,
    subject_kind: Literal["component", "pipeline"],
    reason: str,
) -> HaystackDescriptorDiagnostic:
    return HaystackDescriptorDiagnostic(subject_ref=subject_ref, subject_kind=subject_kind, reason=reason)


def message_to_haystack_chat_message(message: Message) -> dict[str, object]:
    if not isinstance(message, Message):
        raise HaystackBridgeError("message must be a graphblocks Message")
    if (
        len(message.parts) == 1
        and message.parts[0].kind == "text"
        and not message.parts[0].metadata
    ):
        content: object = _wire_string(
            "GraphBlocks text content", message.parts[0].text or ""
        )
    else:
        content_parts: list[dict[str, object]] = []
        for part in message.parts:
            if part.kind == "text":
                projected_part: dict[str, object] = {
                    "type": "text",
                    "text": _wire_string(
                        "GraphBlocks text content", part.text or ""
                    ),
                }
            elif part.kind in {"json", "artifact_ref"}:
                projected_part = {
                    "type": part.kind,
                    "data": deepcopy(dict(part.data or {})),
                }
            else:
                raise HaystackBridgeError(f"unsupported content part kind {part.kind!r}")
            if part.metadata:
                projected_part["graphblocks_metadata"] = deepcopy(
                    dict(part.metadata)
                )
            content_parts.append(projected_part)
        content = content_parts
    meta: dict[str, object] = {"message_id": message.message_id}
    role = message.role
    if role == "developer":
        role = "system"
        meta["graphblocks_role"] = "developer"
    if message.metadata:
        meta["graphblocks_metadata"] = deepcopy(dict(message.metadata))
    return {
        "role": role,
        "content": content,
        "meta": meta,
    }


def haystack_chat_message_to_message(value: Mapping[str, object], *, message_id: str) -> Message:
    value = _mapping_snapshot("haystack chat message", value)
    message_id = _stable_string("message_id", message_id)
    role = value.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise HaystackBridgeError(f"unsupported Haystack chat role {role!r}")
    content_value = value.get("content", "")
    parts: list[ContentPart] = []
    if isinstance(content_value, str):
        try:
            parts.append(
                ContentPart(
                    kind="text",
                    text=_wire_string("Haystack text content", content_value),
                )
            )
        except ValueError as error:
            raise HaystackBridgeError("invalid Haystack text content") from error
    elif isinstance(content_value, (list, tuple)):
        for item in content_value:
            if not isinstance(item, Mapping):
                raise HaystackBridgeError("Haystack content item must be a mapping")
            part_metadata_value = item.get("graphblocks_metadata", {})
            part_metadata = _mapping_snapshot(
                "content part graphblocks_metadata", part_metadata_value
            )
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    parts.append(
                        ContentPart(
                            kind="text",
                            text=_wire_string(
                                "Haystack text content", item["text"]
                            ),
                            metadata=deepcopy(dict(part_metadata)),
                        )
                    )
                except ValueError as error:
                    raise HaystackBridgeError(
                        "invalid Haystack text content item"
                    ) from error
            elif item.get("type") in {"json", "artifact_ref"} and isinstance(item.get("data"), Mapping):
                data = _mapping_snapshot("Haystack content data", item["data"])
                try:
                    parts.append(
                        ContentPart(
                            kind=item["type"],
                            data=deepcopy(dict(data)),
                            metadata=deepcopy(dict(part_metadata)),
                        )
                    )
                except ValueError as error:
                    raise HaystackBridgeError(
                        "invalid Haystack structured content item"
                    ) from error
            else:
                raise HaystackBridgeError("unsupported Haystack content item")
    else:
        raise HaystackBridgeError("Haystack chat content must be a string or list")
    meta = value.get("meta", {})
    if meta is None:
        meta = {}
    if not isinstance(meta, Mapping):
        raise HaystackBridgeError("Haystack chat meta must be a mapping")
    normalized_meta = deepcopy(dict(meta))
    original_role = normalized_meta.pop("graphblocks_role", None)
    if original_role is not None:
        if role != "system" or original_role != "developer":
            raise HaystackBridgeError("invalid GraphBlocks role marker in Haystack chat meta")
        role = "developer"
    graphblocks_metadata = normalized_meta.pop("graphblocks_metadata", None)
    if graphblocks_metadata is None:
        metadata: dict[str, object] = {}
    elif isinstance(graphblocks_metadata, Mapping):
        metadata = deepcopy(dict(graphblocks_metadata))
    else:
        raise HaystackBridgeError(
            "graphblocks_metadata in Haystack chat meta must be a mapping"
        )
    if normalized_meta:
        metadata.setdefault("haystack_meta", normalized_meta)
    try:
        return Message(
            message_id=message_id,
            role=role,  # type: ignore[arg-type]
            parts=tuple(parts),
            metadata=metadata,
        )
    except ValueError as error:
        raise HaystackBridgeError("invalid GraphBlocks message projection") from error


__all__ = [
    "HaystackBridgeError",
    "HaystackComponentBlock",
    "HaystackDescriptorDiagnostic",
    "HaystackPipelineBlock",
    "explicit_descriptor_required",
    "haystack_chat_message_to_message",
    "message_to_haystack_chat_message",
]
