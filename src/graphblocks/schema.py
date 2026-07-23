from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from importlib.resources.abc import Traversable
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError

from .canonical import (
    _MANUAL_INTEGER_BIT_LENGTH,
    _canonical_dumps,
    _has_unicode_surrogate,
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)
from .documents import FrozenList, _freeze_value


SCHEMA_MANIFEST_VERSION = 1
MAX_RESOURCE_DOCUMENT_DEPTH = 64
_U32_MAX_DECIMAL = "4294967295"


class _DiagnosticInteger(int):
    def __repr__(self) -> str:
        return "<arbitrary-size integer>"

    def __str__(self) -> str:
        return "<arbitrary-size integer>"


def _validate_type_without_instance_repr(
    validator: Any,
    expected_types: str | list[str],
    instance: object,
    _schema: Mapping[str, object],
) -> Iterable[ValidationError]:
    type_names = (
        expected_types if isinstance(expected_types, list) else [expected_types]
    )
    if not any(
        validator.is_type(instance, type_name) for type_name in type_names
    ):
        yield ValidationError("value does not have an allowed JSON type")


_ResourceValidator = validators.extend(
    Draft202012Validator,
    {"type": _validate_type_without_instance_repr},
)


class SchemaIdError(ValueError):
    """Raised when a schema identity is not canonical."""


class SchemaManifestError(ValueError):
    """Raised when a JSON Schema manifest cannot be built deterministically."""


class ResourceValidationError(ValueError):
    """Raised when a resource does not satisfy its versioned wire schema."""

    def __init__(self, violations: tuple[ResourceSchemaViolation, ...]) -> None:
        if not violations:
            raise ValueError("resource validation errors require at least one violation")
        self.violations = violations
        summary = "; ".join(
            f"{violation.code} {violation.path}: {violation.message}"
            for violation in violations
        )
        super().__init__(summary)


@dataclass(frozen=True, slots=True, order=True)
class ResourceSchemaViolation:
    """One deterministic resource-schema validation failure."""

    code: str
    path: str
    keyword: str
    message: str
    schema_path: str = "$"


RESOURCE_SCHEMA_PATHS: Mapping[tuple[str, str], str] = {
    ("graphblocks.ai/v1", "Graph"): "graphblocks.ai/v1/graph.schema.json",
    ("graphblocks.ai/v1", "PluginManifest"): "graphblocks.ai/v1/plugin-manifest.schema.json",
    ("graphblocks.ai/v1alpha3", "Graph"): "graphblocks.ai/v1alpha3/graph.schema.json",
    ("graphblocks.ai/v1alpha1", "Application"): "graphblocks.ai/v1alpha1/application.schema.json",
    ("graphblocks.ai/v1alpha1", "Binding"): "graphblocks.ai/v1alpha1/binding.schema.json",
    ("graphblocks.ai/v1alpha1", "PluginManifest"): "graphblocks.ai/v1alpha1/plugin-manifest.schema.json",
    (
        "graphblocks.ai/composition/v1alpha1",
        "GraphFragment",
    ): "graphblocks.ai/composition/v1alpha1/graph-fragment.schema.json",
}


@dataclass(frozen=True, slots=True, order=True)
class SchemaId:
    raw: str
    _version_separator: int = field(init=False, repr=False, compare=False)
    _major_version: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        raw = self.raw
        if not isinstance(raw, str):
            raise SchemaIdError("schema id must be a string")
        if raw == "":
            raise SchemaIdError("schema id must not be empty")
        if _has_unicode_surrogate(raw):
            raise SchemaIdError(
                "schema id must contain only Unicode scalar values"
            )
        if raw.strip() != raw:
            raise SchemaIdError("schema id name is not canonical")
        if any(character.isspace() for character in raw):
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
        if len(version) > len(_U32_MAX_DECIMAL) or (
            len(version) == len(_U32_MAX_DECIMAL)
            and version > _U32_MAX_DECIMAL
        ):
            raise SchemaIdError("schema id major version must be a positive integer")

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
        if not isinstance(self.schema_id, SchemaId):
            raise TypeError("typed value schema_id must be a SchemaId")
        try:
            canonical_value = canonical_loads(
                _canonical_dumps(self.value, reject_tuples=True)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("typed value value must be canonical JSON") from error
        object.__setattr__(
            self,
            "value",
            _freeze_value("typed value", canonical_value),
        )

    @classmethod
    def new(cls, schema_id: str | SchemaId, value: object) -> TypedValue:
        if not isinstance(schema_id, SchemaId):
            schema_id = SchemaId.parse(schema_id)
        return cls(schema_id=schema_id, value=value)

    @classmethod
    def from_value(cls, value: Mapping[str, object]) -> TypedValue:
        if not isinstance(value, Mapping):
            raise TypeError("typed value envelope must be a mapping")
        schema_id = value.get("schema")
        if not isinstance(schema_id, str):
            raise ValueError("typed value schema must be a string")
        if "value" not in value:
            raise ValueError("typed value must include a value field")
        return cls.new(schema_id, value["value"])

    def canonical_value(self) -> dict[str, object]:
        return {
            "schema": self.schema_id.as_str(),
            "value": canonical_loads(canonical_dumps(self.value)),
        }

    def to_json(self) -> str:
        return canonical_dumps(self.canonical_value())


@dataclass(frozen=True, slots=True)
class SchemaManifestEntry:
    schema_id: str
    path: str
    digest: str
    draft: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        _validate_schema_manifest_entry(self)

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


def _validate_schema_manifest_entry(entry: SchemaManifestEntry) -> None:
    if (
        not isinstance(entry.schema_id, str)
        or not entry.schema_id
        or entry.schema_id != entry.schema_id.strip()
        or _has_unicode_surrogate(entry.schema_id)
    ):
        raise SchemaManifestError(
            "schema manifest entries require a non-empty schema id"
        )
    if (
        not isinstance(entry.path, str)
        or not entry.path
        or entry.path != entry.path.strip()
        or _has_unicode_surrogate(entry.path)
    ):
        raise SchemaManifestError(
            f"schema manifest entry {entry.schema_id} requires a relative path"
        )
    path = entry.path
    path_parts = path.split("/")
    canonical_path = PurePosixPath(path).as_posix()
    if (
        path != canonical_path
        or PurePosixPath(path).is_absolute()
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path_parts)
        or re.match(r"^[A-Za-z]:", path) is not None
    ):
        raise SchemaManifestError(
            f"schema manifest entry {entry.schema_id} "
            "requires a safe canonical relative path"
        )
    if not isinstance(entry.digest, str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", entry.digest
    ) is None:
        raise SchemaManifestError(
            f"schema manifest entry {entry.schema_id} requires a sha256 digest"
        )
    for field_name, field_value in (
        ("draft", entry.draft),
        ("title", entry.title),
    ):
        if field_value is not None and (
            not isinstance(field_value, str)
            or _has_unicode_surrogate(field_value)
        ):
            raise SchemaManifestError(
                f"schema manifest entry {entry.schema_id} {field_name} "
                "must be a Unicode scalar string"
            )


@dataclass(frozen=True, slots=True)
class SchemaManifest:
    entries: tuple[SchemaManifestEntry, ...]

    def __post_init__(self) -> None:
        try:
            source_entries = tuple(self.entries)
        except TypeError as error:
            raise SchemaManifestError(
                "schema manifest entries must be an iterable of manifest entries"
            ) from error
        if any(not isinstance(entry, SchemaManifestEntry) for entry in source_entries):
            raise SchemaManifestError(
                "schema manifest entries must contain only SchemaManifestEntry values"
            )
        for entry in source_entries:
            _validate_schema_manifest_entry(entry)
        entries = tuple(
            sorted(source_entries, key=lambda entry: (entry.schema_id, entry.path))
        )
        seen: set[str] = set()
        for entry in entries:
            if entry.schema_id in seen:
                raise SchemaManifestError(f"duplicate schema id {entry.schema_id}")
            seen.add(entry.schema_id)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_directory(cls, root: str | Path) -> SchemaManifest:
        root_path = Path(root)
        if not root_path.is_dir():
            raise SchemaManifestError(f"schema root is not a directory: {root_path}")
        try:
            resolved_root = root_path.resolve(strict=True)
        except OSError as error:
            raise SchemaManifestError(
                f"schema root could not be resolved: {root_path}"
            ) from error
        entries: list[SchemaManifestEntry] = []
        for path in sorted(root_path.rglob("*.json")):
            relative_path = path.relative_to(root_path)
            candidate = root_path
            try:
                for part in relative_path.parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise SchemaManifestError(
                            f"{path}: schema documents must be regular non-symlinked files"
                        )
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
            except SchemaManifestError:
                raise
            except (OSError, ValueError) as error:
                raise SchemaManifestError(
                    f"{path}: schema document must remain within its schema root"
                ) from error
            if not path.is_file():
                raise SchemaManifestError(
                    f"{path}: schema documents must be regular non-symlinked files"
                )
            try:
                document = canonical_loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
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


def _default_resource_schema_root() -> Traversable | Path:
    packaged = resources.files("graphblocks").joinpath("schemas")
    if packaged.is_dir():
        return packaged

    # Editable source installs do not receive hatch's wheel force-include.
    checkout = Path(__file__).resolve().parents[2] / "schemas"
    if checkout.is_dir():
        return checkout
    return packaged


def load_resource_schema(
    api_version: str,
    kind: str,
    *,
    schema_root: str | Path | None = None,
) -> Mapping[str, object]:
    """Load and check the authoritative schema for one resource type.

    The default lookup starts at the schemas embedded in the installed wheel.
    A checkout fallback supports editable installs, and ``schema_root`` gives
    build and conformance tooling an explicit, testable source boundary.
    """

    relative_path = RESOURCE_SCHEMA_PATHS.get((api_version, kind))
    if relative_path is None:
        raise KeyError(f"unsupported resource type {api_version!r}/{kind!r}")

    root = Path(schema_root) if schema_root is not None else _default_resource_schema_root()
    candidate = root.joinpath(*relative_path.split("/"))
    try:
        document = canonical_loads(candidate.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise SchemaManifestError(
            f"cannot load resource schema {relative_path}: {error}"
        ) from error
    if not isinstance(document, Mapping):
        raise SchemaManifestError(f"resource schema {relative_path} must be a JSON object")
    try:
        Draft202012Validator.check_schema(document)
    except SchemaError as error:
        raise SchemaManifestError(
            f"resource schema {relative_path} is not valid draft 2020-12: {error.message}"
        ) from error
    return document


def _json_path(parts: Iterable[object]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            path += f".{part}"
        else:
            path += f"[{canonical_dumps(part)}]"
    return path


def _validation_message(error: ValidationError) -> str:
    keyword = error.validator
    if keyword == "anyOf":
        return "value must match at least one allowed schema"
    if keyword == "oneOf":
        return "value must match exactly one allowed schema"
    if keyword == "not":
        return "value matches a forbidden schema"
    if keyword == "const":
        return f"value must equal {canonical_dumps(error.validator_value)}"
    if keyword == "enum":
        return f"value must be one of {canonical_dumps(error.validator_value)}"
    if keyword == "type":
        return f"value must have JSON type {canonical_dumps(error.validator_value)}"
    if keyword == "uniqueItems":
        return "array items must be unique"
    if keyword == "additionalProperties" and isinstance(error.instance, Mapping):
        declared = error.schema.get("properties", {})
        if isinstance(declared, Mapping):
            unexpected = sorted(str(key) for key in error.instance if key not in declared)
            return f"unexpected properties are not allowed: {canonical_dumps(unexpected)}"
    return error.message


def _schema_violation(error: ValidationError) -> ResourceSchemaViolation:
    keyword = str(error.validator or "schema")
    return ResourceSchemaViolation(
        code="GB0014",
        path=_json_path(error.absolute_path),
        keyword=keyword,
        message=_validation_message(error),
        schema_path=_json_path(error.absolute_schema_path),
    )


def resource_schema_errors(
    document: object,
    *,
    schema_root: str | Path | None = None,
) -> tuple[ResourceSchemaViolation, ...]:
    """Return deterministic violations for a versioned GraphBlocks resource."""

    if not isinstance(document, Mapping):
        return (
            ResourceSchemaViolation(
                code="GB0012",
                path="$",
                keyword="type",
                message="resource must be an object",
            ),
        )

    pending: list[tuple[object, tuple[object, ...], int, bool]] = [
        (document, (), 0, False)
    ]
    active_containers: set[int] = set()
    json_domain_violations: list[ResourceSchemaViolation] = []
    while pending:
        value, path, depth, leaving = pending.pop()
        if leaving:
            active_containers.remove(id(value))
            continue
        if depth > MAX_RESOURCE_DOCUMENT_DEPTH:
            return (
                ResourceSchemaViolation(
                    code="GB0014",
                    path=_json_path(path),
                    keyword="maxDepth",
                    message=(
                        "resource nesting must not exceed "
                        f"{MAX_RESOURCE_DOCUMENT_DEPTH} levels"
                    ),
                ),
            )
        if isinstance(value, Mapping):
            if id(value) in active_containers:
                return (
                    ResourceSchemaViolation(
                        code="GB0014",
                        path=_json_path(path),
                        keyword="recursive",
                        message="resource values must not be recursive",
                    ),
                )
            if any(not isinstance(key, str) for key in value):
                json_domain_violations.append(
                    ResourceSchemaViolation(
                        code="GB0014",
                        path=_json_path(path),
                        keyword="jsonObjectKey",
                        message="resource object keys must be strings",
                    )
                )
            if any(
                isinstance(key, str) and _has_unicode_surrogate(key)
                for key in value
            ):
                json_domain_violations.append(
                    ResourceSchemaViolation(
                        code="GB0014",
                        path=_json_path(path),
                        keyword="unicodeScalar",
                        message=(
                            "resource object keys must contain only Unicode scalar values"
                        ),
                    )
                )
            active_containers.add(id(value))
            pending.append((value, path, depth, True))
            pending.extend(
                (child, (*path, key), depth + 1, False)
                for key, child in sorted(
                    (
                        (key, child)
                        for key, child in value.items()
                        if isinstance(key, str) and not _has_unicode_surrogate(key)
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
            )
        elif isinstance(value, (list, FrozenList)):
            if id(value) in active_containers:
                return (
                    ResourceSchemaViolation(
                        code="GB0014",
                        path=_json_path(path),
                        keyword="recursive",
                        message="resource values must not be recursive",
                    ),
                )
            active_containers.add(id(value))
            pending.append((value, path, depth, True))
            pending.extend(
                (child, (*path, index), depth + 1, False)
                for index, child in enumerate(value)
            )
        elif (
            (isinstance(value, float) and not math.isfinite(value))
            or (isinstance(value, Decimal) and not value.is_finite())
        ):
            json_domain_violations.append(
                ResourceSchemaViolation(
                    code="GB0014",
                    path=_json_path(path),
                    keyword="finiteNumber",
                    message="resource numbers must be finite",
                )
            )
        elif isinstance(value, str) and _has_unicode_surrogate(value):
            json_domain_violations.append(
                ResourceSchemaViolation(
                    code="GB0014",
                    path=_json_path(path),
                    keyword="unicodeScalar",
                    message="resource strings must contain only Unicode scalar values",
                )
            )
        elif not (
            value is None
            or isinstance(value, (str, bool, int, float, Decimal))
        ):
            json_domain_violations.append(
                ResourceSchemaViolation(
                    code="GB0014",
                    path=_json_path(path),
                    keyword="jsonValue",
                    message="resource values must be canonical JSON values",
                )
            )

    if json_domain_violations:
        return tuple(
            sorted(
                json_domain_violations,
                key=lambda violation: (
                    violation.path,
                    violation.keyword,
                    violation.message,
                ),
            )
        )

    envelope_errors: list[ResourceSchemaViolation] = []
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    if not isinstance(api_version, str):
        envelope_errors.append(
            ResourceSchemaViolation(
                code="GB0012",
                path="$.apiVersion",
                keyword="type",
                message="apiVersion must be a string",
            )
        )
    if not isinstance(kind, str):
        envelope_errors.append(
            ResourceSchemaViolation(
                code="GB0012",
                path="$.kind",
                keyword="type",
                message="kind must be a string",
            )
        )
    if envelope_errors:
        return tuple(envelope_errors)

    if (api_version, kind) not in RESOURCE_SCHEMA_PATHS:
        return (
            ResourceSchemaViolation(
                code="GB0013",
                path="$",
                keyword="resourceType",
                message=f"unsupported resource type {api_version!r}/{kind!r}",
            ),
        )

    schema = load_resource_schema(api_version, kind, schema_root=schema_root)
    validation_document = deepcopy(document)
    validation_values: list[object] = [validation_document]
    while validation_values:
        value = validation_values.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    isinstance(child, int)
                    and not isinstance(child, bool)
                    and child.bit_length() > _MANUAL_INTEGER_BIT_LENGTH
                ):
                    value[key] = _DiagnosticInteger(child)
                elif isinstance(child, (dict, list)):
                    validation_values.append(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if (
                    isinstance(child, int)
                    and not isinstance(child, bool)
                    and child.bit_length() > _MANUAL_INTEGER_BIT_LENGTH
                ):
                    value[index] = _DiagnosticInteger(child)
                elif isinstance(child, (dict, list)):
                    validation_values.append(child)
    validator = _ResourceValidator(schema)
    violations = [
        _schema_violation(error)
        for error in validator.iter_errors(validation_document)
    ]
    return tuple(
        sorted(
            violations,
            key=lambda violation: (
                violation.path,
                violation.schema_path,
                violation.keyword,
                violation.message,
            ),
        )
    )


def validate_resource(
    document: object,
    *,
    schema_root: str | Path | None = None,
) -> None:
    """Validate a resource against its exact ``apiVersion``/``kind`` schema."""

    violations = resource_schema_errors(document, schema_root=schema_root)
    if violations:
        raise ResourceValidationError(violations)
