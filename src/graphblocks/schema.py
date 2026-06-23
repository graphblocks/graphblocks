from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .canonical import canonical_dumps


class SchemaIdError(ValueError):
    """Raised when a schema identity is not canonical."""


@dataclass(frozen=True, slots=True, order=True)
class SchemaId:
    raw: str
    _version_separator: int = field(init=False, repr=False, compare=False)
    _major_version: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        raw = self.raw
        if raw == "":
            raise SchemaIdError("schema id must not be empty")
        if raw.strip() != raw:
            raise SchemaIdError("schema id name is not canonical")

        name, separator, version = raw.rpartition("@")
        if separator == "":
            raise SchemaIdError("schema id must include a major version suffix")
        if name == "":
            raise SchemaIdError("schema id name must not be empty")
        if version == "" or not all("0" <= char <= "9" for char in version):
            raise SchemaIdError("schema id major version must be a positive integer")
        if len(version) > 1 and version.startswith("0"):
            raise SchemaIdError("schema id major version must not use leading zeroes")

        major_version = int(version)
        if major_version == 0:
            raise SchemaIdError("schema id major version must be a positive integer")

        object.__setattr__(self, "_version_separator", len(name))
        object.__setattr__(self, "_major_version", major_version)

    @classmethod
    def parse(cls, raw: str) -> SchemaId:
        return cls(raw)

    def as_str(self) -> str:
        return self.raw

    @property
    def name(self) -> str:
        return self.raw[: self._version_separator]

    @property
    def major_version(self) -> int:
        return self._major_version

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True, slots=True)
class TypedValue:
    schema_id: SchemaId
    value: object

    @classmethod
    def new(cls, schema_id: str | SchemaId, value: object) -> TypedValue:
        if not isinstance(schema_id, SchemaId):
            schema_id = SchemaId.parse(schema_id)
        return cls(schema_id=schema_id, value=value)

    @classmethod
    def from_value(cls, value: Mapping[str, object]) -> TypedValue:
        schema_id = value.get("schema")
        if not isinstance(schema_id, str):
            raise ValueError("typed value schema must be a string")
        if "value" not in value:
            raise ValueError("typed value must include a value field")
        return cls.new(schema_id, value["value"])

    def canonical_value(self) -> dict[str, object]:
        return {"schema": self.schema_id.as_str(), "value": self.value}

    def to_json(self) -> str:
        return canonical_dumps(self.canonical_value())
