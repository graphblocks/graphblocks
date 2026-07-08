from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path

from .canonical import canonical_dumps, canonical_hash


SCHEMA_MANIFEST_VERSION = 1


class SchemaIdError(ValueError):
    """Raised when a schema identity is not canonical."""


class SchemaManifestError(ValueError):
    """Raised when a JSON Schema manifest cannot be built deterministically."""


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

    def __post_init__(self) -> None:
        stack: list[object] = [self.value]
        while stack:
            current = stack.pop()
            if current is None or isinstance(current, (str, bool, int, float)):
                continue
            if isinstance(current, list):
                stack.extend(current)
                continue
            if isinstance(current, dict):
                for key, nested in current.items():
                    if not isinstance(key, str):
                        raise ValueError("typed value value must be canonical JSON")
                    stack.append(nested)
                continue
            raise ValueError("typed value value must be canonical JSON")

        try:
            canonical_value = json.loads(canonical_dumps(self.value))
        except (TypeError, ValueError) as error:
            raise ValueError("typed value value must be canonical JSON") from error
        object.__setattr__(self, "value", canonical_value)

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
        return {"schema": self.schema_id.as_str(), "value": json.loads(canonical_dumps(self.value))}

    def to_json(self) -> str:
        return canonical_dumps(self.canonical_value())


@dataclass(frozen=True, slots=True)
class SchemaManifestEntry:
    schema_id: str
    path: str
    digest: str
    draft: str | None = None
    title: str | None = None

    def manifest_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "schemaId": self.schema_id,
            "path": self.path,
            "digest": self.digest,
        }
        if self.draft is not None:
            entry["draft"] = self.draft
        if self.title is not None:
            entry["title"] = self.title
        return entry


@dataclass(frozen=True, slots=True)
class SchemaManifest:
    entries: tuple[SchemaManifestEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries, key=lambda entry: (entry.schema_id, entry.path)))
        seen: set[str] = set()
        for entry in entries:
            if not entry.schema_id.strip():
                raise SchemaManifestError("schema manifest entries require a non-empty schema id")
            if not entry.path.strip():
                raise SchemaManifestError(f"schema manifest entry {entry.schema_id} requires a relative path")
            if not entry.digest.startswith("sha256:"):
                raise SchemaManifestError(f"schema manifest entry {entry.schema_id} requires a sha256 digest")
            if entry.schema_id in seen:
                raise SchemaManifestError(f"duplicate schema id {entry.schema_id}")
            seen.add(entry.schema_id)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_directory(cls, root: str | Path) -> SchemaManifest:
        root_path = Path(root)
        if not root_path.is_dir():
            raise SchemaManifestError(f"schema root is not a directory: {root_path}")
        entries: list[SchemaManifestEntry] = []
        for path in sorted(root_path.rglob("*.json")):
            try:
                document = json.loads(
                    path.read_text(encoding="utf-8"),
                    parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
                )
            except ValueError as error:
                raise SchemaManifestError(f"{path}: invalid strict JSON schema document") from error
            if not isinstance(document, Mapping):
                raise SchemaManifestError(f"{path}: JSON schema document must be an object")
            schema_id = document.get("$id")
            if not isinstance(schema_id, str) or not schema_id.strip():
                raise SchemaManifestError(f"{path}: JSON schema document must declare a string $id")
            draft = document.get("$schema")
            if draft is not None and not isinstance(draft, str):
                raise SchemaManifestError(f"{path}: JSON schema $schema must be a string")
            title = document.get("title")
            if title is not None and not isinstance(title, str):
                raise SchemaManifestError(f"{path}: JSON schema title must be a string")
            entries.append(
                SchemaManifestEntry(
                    schema_id=schema_id,
                    path=path.relative_to(root_path).as_posix(),
                    digest=canonical_hash(document),
                    draft=draft,
                    title=title,
                )
            )
        if not entries:
            raise SchemaManifestError(f"schema root contains no JSON schema documents: {root_path}")
        return cls(tuple(entries))

    def manifest_contract(self) -> dict[str, object]:
        return {
            "manifestVersion": SCHEMA_MANIFEST_VERSION,
            "schemas": [entry.manifest_entry() for entry in self.entries],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.manifest_contract())

    def manifest_payload(self) -> dict[str, object]:
        return self.manifest_contract() | {"contentDigest": self.content_digest()}

    def to_json(self) -> str:
        return canonical_dumps(self.manifest_payload())
