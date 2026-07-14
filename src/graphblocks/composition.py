from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

import yaml

from .canonical import _normalize_graph_unchecked, canonical_hash
from .migration import (
    GRAPH_API_VERSION,
    LEGACY_GRAPH_API_VERSIONS,
    MigrationError,
    migrate_document,
)
from .schema import SchemaId, SchemaIdError


COMPOSITION_API_VERSION = "graphblocks.ai/composition/v1alpha1"
MAX_IMPORT_DEPTH = 32
MAX_SOURCE_COUNT = 256
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_COUNT = 1024
MAX_EXPANDED_NODES = 10_000
MAX_EXPANDED_EDGES = 50_000
MAX_YAML_VALUES = 100_000
MAX_YAML_DEPTH = 128
COMPOSITION_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class CompositionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "$",
        source: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.source = source
        location = f"{source}:" if source is not None else ""
        super().__init__(f"{code} {location}{path}: {message}")

    def to_diagnostic(self) -> dict[str, str]:
        diagnostic = {
            "severity": "error",
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.source is not None:
            diagnostic["source"] = self.source
        return diagnostic


@dataclass(frozen=True, slots=True, order=True)
class CompositionSource:
    path: str
    digest: str

    def canonical_value(self) -> dict[str, str]:
        return {"path": self.path, "digest": self.digest}


@dataclass(frozen=True, slots=True, order=True)
class CompositionInstance:
    graph: str
    node: str
    fragment: str
    source: str

    def canonical_value(self) -> dict[str, str]:
        return {
            "graph": self.graph,
            "node": self.node,
            "fragment": self.fragment,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CompositionReport:
    sources: tuple[CompositionSource, ...]
    instances: tuple[CompositionInstance, ...]
    composition_digest: str

    @classmethod
    def create(
        cls,
        sources: tuple[CompositionSource, ...],
        instances: tuple[CompositionInstance, ...],
    ) -> CompositionReport:
        ordered_sources = tuple(sorted(sources))
        ordered_instances = tuple(sorted(instances))
        digest = canonical_hash(
            {
                "apiVersion": COMPOSITION_API_VERSION,
                "sources": [source.canonical_value() for source in ordered_sources],
                "instances": [instance.canonical_value() for instance in ordered_instances],
            }
        )
        return cls(ordered_sources, ordered_instances, digest)

    def canonical_value(self) -> dict[str, object]:
        return {
            "apiVersion": COMPOSITION_API_VERSION,
            "sources": [source.canonical_value() for source in self.sources],
            "instances": [instance.canonical_value() for instance in self.instances],
            "compositionDigest": self.composition_digest,
        }


@dataclass(frozen=True, slots=True)
class CompositionResult:
    documents: tuple[dict[str, Any], ...]
    report: CompositionReport


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise CompositionError(
                "CompositionInvalidYaml",
                "mapping keys must be scalar JSON strings",
            ) from error
        if duplicate:
            raise CompositionError(
                "CompositionDuplicateKey",
                f"duplicate YAML mapping key {key!r}",
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _validate_json_value(value: object, *, source: str) -> None:
    pending: list[tuple[object, frozenset[int], str, int]] = [
        (value, frozenset(), "$", 0)
    ]
    visited_values = 0
    while pending:
        current, ancestors, path, depth = pending.pop()
        visited_values += 1
        if visited_values > MAX_YAML_VALUES:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"YAML value count exceeds {MAX_YAML_VALUES}",
                path=path,
                source=source,
            )
        if depth > MAX_YAML_DEPTH:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"YAML value depth exceeds {MAX_YAML_DEPTH}",
                path=path,
                source=source,
            )
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise CompositionError(
                    "CompositionInvalidYaml",
                    "YAML numbers must be finite",
                    path=path,
                    source=source,
                )
            continue
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in ancestors:
                raise CompositionError(
                    "CompositionInvalidYaml",
                    "recursive YAML aliases are not allowed",
                    path=path,
                    source=source,
                )
            next_ancestors = ancestors | {identity}
            if isinstance(current, dict):
                for key, child in current.items():
                    if not isinstance(key, str):
                        raise CompositionError(
                            "CompositionInvalidYaml",
                            "YAML mapping keys must be strings",
                            path=path,
                            source=source,
                        )
                    pending.append((child, next_ancestors, f"{path}.{key}", depth + 1))
            else:
                for index, child in enumerate(current):
                    pending.append((child, next_ancestors, f"{path}[{index}]", depth + 1))
            continue
        raise CompositionError(
            "CompositionInvalidYaml",
            f"YAML value {type(current).__name__} is outside the canonical JSON domain",
            path=path,
            source=source,
        )


def _validate_yaml_event_depth(text: str, *, source: str) -> None:
    depth = 0
    try:
        for event in yaml.parse(text, Loader=_StrictSafeLoader):
            if isinstance(
                event,
                (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent),
            ):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise CompositionError(
                        "CompositionLimitExceeded",
                        f"YAML value depth exceeds {MAX_YAML_DEPTH}",
                        source=source,
                    )
            elif isinstance(
                event,
                (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent),
            ):
                depth -= 1
    except CompositionError:
        raise
    except RecursionError as error:
        raise CompositionError(
            "CompositionLimitExceeded",
            f"YAML parser depth exceeds {MAX_YAML_DEPTH}",
            source=source,
        ) from error
    except yaml.YAMLError as error:
        raise CompositionError(
            "CompositionInvalidYaml",
            str(error),
            source=source,
        ) from error


@dataclass(frozen=True, slots=True)
class _SourceDocument:
    source: Path
    logical_source: str
    index: int
    value: dict[str, Any]


class _Composer:
    def __init__(self, entry: Path, root: Path) -> None:
        self.entry = entry
        self.root = root
        self._documents_by_source: dict[Path, tuple[_SourceDocument, ...]] = {}
        self._source_bytes = 0
        self._source_digests: dict[Path, str] = {}
        self._document_count = 0
        self._load_stack: list[Path] = []
        self._instances: list[CompositionInstance] = []

    def compose(self) -> CompositionResult:
        entry_documents = self._load_source(self.entry, depth=0)
        emitted: list[dict[str, Any]] = []
        imported_bindings: list[dict[str, Any]] = []
        fragment_identities: set[tuple[Path, str]] = set()

        for document in entry_documents:
            if document.value.get("kind") == "GraphFragment":
                raise CompositionError(
                    "CompositionUnsupportedKind",
                    "GraphFragment documents must be imported and cannot appear in the entry stream",
                    path=f"$[{document.index}].kind",
                    source=document.logical_source,
                )

        for source, documents in sorted(
            self._documents_by_source.items(),
            key=lambda item: self._logical_path(item[0]),
        ):
            if source == self.entry:
                continue
            for document in documents:
                kind = document.value.get("kind")
                if kind == "Binding":
                    _validate_imported_binding(document)
                    imported_bindings.append(deepcopy(document.value))
                elif kind == "GraphFragment":
                    _validate_fragment_envelope(document)
                    fragment_name = document.value["metadata"]["name"]
                    fragment_identity = (source, fragment_name)
                    if fragment_identity in fragment_identities:
                        raise CompositionError(
                            "CompositionDuplicateIdentity",
                            f"duplicate GraphFragment name {fragment_name!r} in one imported stream",
                            source=document.logical_source,
                        )
                    fragment_identities.add(fragment_identity)
                else:
                    raise CompositionError(
                        "CompositionUnsupportedKind",
                        "imported YAML may contain only GraphFragment and Binding documents",
                        path=f"$[{document.index}].kind",
                        source=document.logical_source,
                    )

        for document in entry_documents:
            if document.value.get("kind") == "Graph":
                emitted.append(self._expand_graph(document))
            else:
                emitted.append(deepcopy(document.value))

        imported_bindings.sort(key=_resource_sort_key)
        emitted.extend(imported_bindings)
        self._reject_duplicate_resources(emitted)

        sources = tuple(
            CompositionSource(
                path=self._logical_path(path),
                digest=self._source_digests[path],
            )
            for path in self._documents_by_source
        )
        report = CompositionReport.create(sources, tuple(self._instances))
        return CompositionResult(tuple(emitted), report)

    def _load_source(self, source: Path, *, depth: int) -> tuple[_SourceDocument, ...]:
        if depth > MAX_IMPORT_DEPTH:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"import depth exceeds {MAX_IMPORT_DEPTH}",
                source=self._logical_path(source),
            )
        if source in self._load_stack:
            cycle = self._load_stack[self._load_stack.index(source) :] + [source]
            chain = " -> ".join(self._logical_path(item) for item in cycle)
            raise CompositionError(
                "CompositionImportCycle",
                f"import cycle detected: {chain}",
                source=self._logical_path(source),
            )
        cached = self._documents_by_source.get(source)
        if cached is not None:
            return cached
        known_sources = set(self._documents_by_source) | set(self._load_stack)
        if len(known_sources) >= MAX_SOURCE_COUNT:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"source count exceeds {MAX_SOURCE_COUNT}",
                source=self._logical_path(source),
            )

        try:
            relative_path = source.relative_to(self.root)
        except ValueError as error:
            raise CompositionError(
                "CompositionImportOutsideRoot",
                "composition source escapes the composition root",
                source=self._logical_path(source),
            ) from error

        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        source_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOCTTY", 0)
            | no_follow
        )
        supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
        reparse_point_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        data: bytes | None = None
        if supports_dir_fd and no_follow:
            opened_directories: list[int] = []
            file_fd: int | None = None
            try:
                root_stat = os.lstat(self.root)
                if stat.S_ISLNK(root_stat.st_mode) or bool(
                    getattr(root_stat, "st_file_attributes", 0) & reparse_point_flag
                ):
                    raise CompositionError(
                        "CompositionSymlinkRejected",
                        "composition sources must not traverse symbolic links",
                        source=self._logical_path(source),
                    )
                directory_fd = os.open(self.root, directory_flags | no_follow)
                opened_directories.append(directory_fd)
                if not stat.S_ISDIR(os.fstat(directory_fd).st_mode) or not os.path.samestat(
                    root_stat, os.fstat(directory_fd)
                ):
                    raise OSError("composition root changed while it was opened")
                for part in relative_path.parts[:-1]:
                    directory_fd = os.open(
                        part,
                        directory_flags | no_follow,
                        dir_fd=directory_fd,
                    )
                    opened_directories.append(directory_fd)
                    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                        raise NotADirectoryError(part)
                if not relative_path.parts:
                    raise IsADirectoryError(str(source))
                file_fd = os.open(
                    relative_path.parts[-1],
                    source_flags,
                    dir_fd=directory_fd,
                )
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise OSError(f"composition source is not a regular file: {source}")
                with os.fdopen(file_fd, "rb") as stream:
                    file_fd = None
                    data = stream.read(MAX_SOURCE_BYTES + 1)
            except CompositionError:
                raise
            except (NotImplementedError, TypeError):
                data = None
            except OSError as error:
                try:
                    became_link = stat.S_ISLNK(os.lstat(source).st_mode)
                except OSError:
                    became_link = False
                if became_link:
                    raise CompositionError(
                        "CompositionSymlinkRejected",
                        "composition sources must not traverse symbolic links",
                        source=self._logical_path(source),
                    ) from error
                raise CompositionError(
                    "CompositionInvalidImport",
                    "composition source could not be read safely",
                    source=self._logical_path(source),
                ) from error
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                for opened_fd in reversed(opened_directories):
                    os.close(opened_fd)

        if data is None:
            path_components = [self.root]
            current_path = self.root
            for part in relative_path.parts:
                current_path /= part
                path_components.append(current_path)

            component_stats: list[os.stat_result] = []
            try:
                for index, path_component in enumerate(path_components):
                    component_stat = os.lstat(path_component)
                    is_reparse_point = bool(
                        getattr(component_stat, "st_file_attributes", 0)
                        & reparse_point_flag
                    )
                    if stat.S_ISLNK(component_stat.st_mode) or is_reparse_point:
                        raise CompositionError(
                            "CompositionSymlinkRejected",
                            "composition sources must not traverse symbolic links",
                            source=self._logical_path(source),
                        )
                    if index < len(path_components) - 1:
                        if not stat.S_ISDIR(component_stat.st_mode):
                            raise NotADirectoryError(str(path_component))
                    elif not stat.S_ISREG(component_stat.st_mode):
                        raise OSError(
                            f"composition source is not a regular file: {source}"
                        )
                    if (component_stat.st_dev, component_stat.st_ino) == (0, 0):
                        raise OSError(
                            f"composition source identity cannot be verified: {path_component}"
                        )
                    component_stats.append(component_stat)

                file_fd = None
                try:
                    file_fd = os.open(
                        source,
                        source_flags,
                    )
                    opened_stat = os.fstat(file_fd)
                    if not stat.S_ISREG(opened_stat.st_mode):
                        raise OSError(
                            f"composition source is not a regular file: {source}"
                        )
                    opened_path = source.resolve(strict=True)
                    if not opened_path.is_relative_to(self.root):
                        raise CompositionError(
                            "CompositionImportOutsideRoot",
                            "composition source escapes the composition root",
                            source=self._logical_path(source),
                        )
                    if opened_path != source or not os.path.samestat(
                        component_stats[-1], opened_stat
                    ):
                        raise OSError(
                            f"composition source changed while it was opened: {source}"
                        )
                    with os.fdopen(file_fd, "rb") as stream:
                        file_fd = None
                        data = stream.read(MAX_SOURCE_BYTES + 1)
                finally:
                    if file_fd is not None:
                        os.close(file_fd)

                final_path = source.resolve(strict=True)
                if not final_path.is_relative_to(self.root):
                    raise CompositionError(
                        "CompositionImportOutsideRoot",
                        "composition source escapes the composition root",
                        source=self._logical_path(source),
                    )
                if final_path != source:
                    raise OSError(f"composition source changed while it was read: {source}")
                for path_component, initial_stat in zip(
                    path_components, component_stats, strict=True
                ):
                    final_stat = os.lstat(path_component)
                    is_reparse_point = bool(
                        getattr(final_stat, "st_file_attributes", 0)
                        & reparse_point_flag
                    )
                    if stat.S_ISLNK(final_stat.st_mode) or is_reparse_point:
                        raise CompositionError(
                            "CompositionSymlinkRejected",
                            "composition sources must not traverse symbolic links",
                            source=self._logical_path(source),
                        )
                    if not os.path.samestat(initial_stat, final_stat):
                        raise OSError(
                            f"composition source path changed while it was read: {path_component}"
                        )
            except CompositionError:
                raise
            except OSError as error:
                raise CompositionError(
                    "CompositionInvalidImport",
                    "composition source could not be read safely",
                    source=self._logical_path(source),
                ) from error

        if data is None:
            raise CompositionError(
                "CompositionInvalidImport",
                "composition source could not be read safely",
                source=self._logical_path(source),
            )
        if len(data) > MAX_SOURCE_BYTES:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"source size exceeds {MAX_SOURCE_BYTES} bytes",
                source=self._logical_path(source),
            )
        self._source_bytes += len(data)
        self._source_digests[source] = "sha256:" + hashlib.sha256(data).hexdigest()
        if self._source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"total source size exceeds {MAX_TOTAL_SOURCE_BYTES} bytes",
                source=self._logical_path(source),
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CompositionError(
                "CompositionInvalidYaml",
                "composition sources must be UTF-8",
                source=self._logical_path(source),
            ) from error
        _validate_yaml_event_depth(text, source=self._logical_path(source))
        try:
            raw_documents = [
                document
                for document in yaml.load_all(text, Loader=_StrictSafeLoader)
                if document is not None
            ]
        except CompositionError as error:
            if error.source is not None:
                raise
            raise CompositionError(
                error.code,
                error.message,
                path=error.path,
                source=self._logical_path(source),
            ) from error
        except yaml.YAMLError as error:
            raise CompositionError(
                "CompositionInvalidYaml",
                str(error),
                source=self._logical_path(source),
            ) from error
        except RecursionError as error:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"YAML parser depth exceeds {MAX_YAML_DEPTH}",
                source=self._logical_path(source),
            ) from error

        documents: list[_SourceDocument] = []
        for index, value in enumerate(raw_documents):
            if not isinstance(value, dict):
                raise CompositionError(
                    "CompositionInvalidYaml",
                    "each YAML document must be a mapping",
                    path=f"$[{index}]",
                    source=self._logical_path(source),
                )
            _validate_json_value(value, source=self._logical_path(source))
            documents.append(
                _SourceDocument(
                    source=source,
                    logical_source=self._logical_path(source),
                    index=index,
                    value=value,
                )
            )
        self._document_count += len(documents)
        if self._document_count > MAX_DOCUMENT_COUNT:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"document count exceeds {MAX_DOCUMENT_COUNT}",
                source=self._logical_path(source),
            )

        loaded_documents = tuple(documents)
        self._load_stack.append(source)
        try:
            for document in loaded_documents:
                if _composition_mapping(document) is not None and document.value.get("kind") != "Graph":
                    raise CompositionError(
                        "CompositionUnsupportedKind",
                        "only Graph documents may declare spec.composition in v1alpha1",
                        path="$.spec.composition",
                        source=document.logical_source,
                    )
                if source != self.entry and _composition_mapping(document) is not None:
                    raise CompositionError(
                        "CompositionUnsupportedKind",
                        "only entry Graph documents may declare spec.composition in v1alpha1",
                        path="$.spec.composition",
                        source=document.logical_source,
                    )
                for imported_source in self._import_paths(document):
                    self._load_source(imported_source, depth=depth + 1)
        finally:
            self._load_stack.pop()
        self._documents_by_source[source] = loaded_documents
        return loaded_documents

    def _import_paths(self, document: _SourceDocument) -> tuple[Path, ...]:
        composition = _composition_mapping(document)
        if composition is None:
            return ()
        imports = composition.get("imports", {})
        if not isinstance(imports, dict):
            raise CompositionError(
                "CompositionInvalidImport",
                "composition.imports must be a mapping",
                path="$.spec.composition.imports",
                source=document.logical_source,
            )
        resolved: list[Path] = []
        for alias in sorted(imports):
            if not isinstance(alias, str) or COMPOSITION_NAME_PATTERN.fullmatch(alias) is None:
                raise CompositionError(
                    "CompositionInvalidImport",
                    "import aliases must be non-empty names without '/' or '$' prefixes",
                    path="$.spec.composition.imports",
                    source=document.logical_source,
                )
            entry = imports[alias]
            if not isinstance(entry, dict) or set(entry) != {"path"}:
                raise CompositionError(
                    "CompositionInvalidImport",
                    "each import must contain only a path field",
                    path=f"$.spec.composition.imports.{alias}",
                    source=document.logical_source,
                )
            raw_path = entry.get("path")
            if not isinstance(raw_path, str):
                raise CompositionError(
                    "CompositionInvalidImport",
                    "import path must be a string",
                    path=f"$.spec.composition.imports.{alias}.path",
                    source=document.logical_source,
                )
            resolved.append(self._resolve_import(document.source, raw_path, alias))
        return tuple(resolved)

    def _resolve_import(self, owner: Path, raw_path: str, alias: str) -> Path:
        import_path = PurePosixPath(raw_path)
        invalid = (
            raw_path == ""
            or "\\" in raw_path
            or import_path.is_absolute()
            or any(part in {"", ".", ".."} for part in import_path.parts)
            or raw_path.startswith("~")
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw_path) is not None
            or any(character in raw_path for character in "*?[")
        )
        if invalid:
            raise CompositionError(
                "CompositionInvalidImport",
                "import paths must be literal relative POSIX paths without traversal, URI, or glob syntax",
                path=f"$.spec.composition.imports.{alias}.path",
                source=self._logical_path(owner),
            )
        candidate = owner.parent.joinpath(*import_path.parts)
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise CompositionError(
                "CompositionImportOutsideRoot",
                "import path escapes the composition root",
                path=f"$.spec.composition.imports.{alias}.path",
                source=self._logical_path(owner),
            ) from error
        current = self.root
        relative = candidate.relative_to(self.root)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise CompositionError(
                    "CompositionSymlinkRejected",
                    "composition imports must not traverse symbolic links",
                    path=f"$.spec.composition.imports.{alias}.path",
                    source=self._logical_path(owner),
                )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise CompositionError(
                "CompositionInvalidImport",
                f"import source does not exist: {raw_path}",
                path=f"$.spec.composition.imports.{alias}.path",
                source=self._logical_path(owner),
            ) from error
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise CompositionError(
                "CompositionImportOutsideRoot",
                "resolved import path escapes the composition root",
                path=f"$.spec.composition.imports.{alias}.path",
                source=self._logical_path(owner),
            ) from error
        if not resolved.is_file() or resolved.suffix.lower() not in {".yaml", ".yml"}:
            raise CompositionError(
                "CompositionInvalidImport",
                "imports must resolve to regular .yaml or .yml files",
                path=f"$.spec.composition.imports.{alias}.path",
                source=self._logical_path(owner),
            )
        return resolved

    def _expand_graph(self, document: _SourceDocument) -> dict[str, Any]:
        composition = _composition_mapping(document)
        source_spec = document.value.get("spec")
        source_nodes = source_spec.get("nodes", {}) if isinstance(source_spec, dict) else {}
        if not isinstance(source_nodes, dict):
            source_nodes = {}
        source_slot_nodes = [
            name
            for name, node in source_nodes.items()
            if isinstance(node, dict) and "slot" in node
        ]
        api_version = document.value.get("apiVersion")
        if api_version not in {GRAPH_API_VERSION, *LEGACY_GRAPH_API_VERSIONS}:
            raise CompositionError(
                "CompositionUnsupportedVersion",
                f"Graph apiVersion {api_version!r} is unsupported",
                path="$.apiVersion",
                source=document.logical_source,
            )
        if composition is not None and api_version != "graphblocks.ai/v1alpha3":
            raise CompositionError(
                "CompositionUnsupportedVersion",
                "composed root Graph documents must use 'graphblocks.ai/v1alpha3'",
                path="$.apiVersion",
                source=document.logical_source,
            )
        source_graph = deepcopy(document.value)
        try:
            source_graph = migrate_document(source_graph)
        except MigrationError as error:
            # Composition and slot placeholders are authoring-only alpha
            # fields. Migration is retried after expansion removes them.
            if api_version not in LEGACY_GRAPH_API_VERSIONS:
                raise CompositionError(
                    "CompositionUnsupportedVersion",
                    error.message,
                    path=error.path,
                    source=document.logical_source,
                ) from error
        source_graph = _normalize_graph_unchecked(source_graph)
        if composition is None:
            if source_slot_nodes:
                raise CompositionError(
                    "CompositionUnknownSlot",
                    "slot nodes require spec.composition",
                    path=f"$.spec.nodes.{source_slot_nodes[0]}.slot",
                    source=document.logical_source,
                )
            return source_graph

        graph = source_graph
        spec = graph.get("spec")
        if not isinstance(spec, dict):
            return graph
        nodes = spec.get("nodes", {})
        edges = spec.get("edges", [])
        if not isinstance(nodes, dict) or not isinstance(edges, list):
            return graph
        slot_nodes = [
            name
            for name, node in nodes.items()
            if isinstance(node, dict) and "slot" in node
        ]
        slots = composition.get("slots", {})
        if not isinstance(slots, dict):
            raise CompositionError(
                "CompositionUnknownSlot",
                "composition.slots must be a mapping",
                path="$.spec.composition.slots",
                source=document.logical_source,
            )
        for slot_name in slots:
            if not isinstance(slot_name, str) or COMPOSITION_NAME_PATTERN.fullmatch(slot_name) is None:
                raise CompositionError(
                    "CompositionUnknownSlot",
                    "slot names must match ^[A-Za-z][A-Za-z0-9_-]*$",
                    path="$.spec.composition.slots",
                    source=document.logical_source,
                )
        imports = self._alias_sources(document)
        graph_name = _metadata_name(graph, document)
        resolved_slots = {
            slot_name: self._resolve_slot(document, slot_name, slots[slot_name], imports)
            for slot_name in sorted(slots)
        }

        for node_name in sorted(slot_nodes):
            if COMPOSITION_NAME_PATTERN.fullmatch(node_name) is None:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    "slot instance names must match ^[A-Za-z][A-Za-z0-9_-]*$",
                    path=f"$.spec.nodes.{node_name}",
                    source=document.logical_source,
                )
            raw_node = nodes[node_name]
            assert isinstance(raw_node, dict)
            if set(raw_node) != {"slot"}:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    "a normalized slot node may contain only the slot field",
                    path=f"$.spec.nodes.{node_name}",
                    source=document.logical_source,
                )
            slot_name = raw_node.get("slot")
            if not isinstance(slot_name, str) or slot_name not in slots:
                raise CompositionError(
                    "CompositionUnknownSlot",
                    f"unknown slot {slot_name!r}",
                    path=f"$.spec.nodes.{node_name}.slot",
                    source=document.logical_source,
                )
            fragment_ref, fragment_document, slot_interface, fragment_spec = resolved_slots[
                slot_name
            ]
            _alias, fragment_name = fragment_ref.split("/", 1)

            fragment_graph = {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": fragment_name},
                "spec": deepcopy(fragment_spec),
            }
            try:
                fragment_graph = migrate_document(fragment_graph)
            except MigrationError:
                # GraphFragment v1 intentionally admits preview node fields.
                # Keep those fragments on the alpha wire instead of forcing a
                # stable envelope that cannot represent them.
                pass
            fragment_graph = _normalize_graph_unchecked(fragment_graph)
            fragment_graph_spec = fragment_graph["spec"]
            fragment_nodes = fragment_graph_spec.get("nodes", {})
            fragment_edges = fragment_graph_spec.get("edges", [])
            if not isinstance(fragment_nodes, dict) or not isinstance(fragment_edges, list):
                raise CompositionError(
                    "CompositionInvalidWiring",
                    "fragment nodes and edges must be collections",
                    source=fragment_document.logical_source,
                )
            self._splice_fragment(
                graph_name=graph_name,
                graph_source=document.logical_source,
                node_name=node_name,
                fragment_ref=fragment_ref,
                fragment_source=fragment_document.logical_source,
                interface=slot_interface,
                nodes=nodes,
                edges=edges,
                fragment_nodes=fragment_nodes,
                fragment_edges=fragment_edges,
            )

        spec.pop("composition", None)
        if len(nodes) > MAX_EXPANDED_NODES:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"expanded node count exceeds {MAX_EXPANDED_NODES}",
                path="$.spec.nodes",
                source=document.logical_source,
            )
        if len(edges) > MAX_EXPANDED_EDGES:
            raise CompositionError(
                "CompositionLimitExceeded",
                f"expanded edge count exceeds {MAX_EXPANDED_EDGES}",
                path="$.spec.edges",
                source=document.logical_source,
            )
        try:
            graph = migrate_document(graph)
        except MigrationError:
            # A composed graph containing preview-only fragment fields remains
            # an alpha graph; stable-representable results migrate to v1.
            pass
        return _normalize_graph_unchecked(graph)

    def _resolve_slot(
        self,
        document: _SourceDocument,
        slot_name: str,
        slot: object,
        imports: Mapping[str, Path],
    ) -> tuple[str, _SourceDocument, object, dict[str, Any]]:
        if not isinstance(slot, dict) or set(slot) != {"interface", "fill"}:
            raise CompositionError(
                "CompositionUnfilledSlot",
                "slot declarations require exactly interface and fill",
                path=f"$.spec.composition.slots.{slot_name}",
                source=document.logical_source,
            )
        fill = slot.get("fill")
        if not isinstance(fill, dict) or set(fill) != {"fragment"}:
            raise CompositionError(
                "CompositionUnfilledSlot",
                "slot fill must contain exactly one fragment reference",
                path=f"$.spec.composition.slots.{slot_name}.fill",
                source=document.logical_source,
            )
        fragment_ref = fill.get("fragment")
        if not isinstance(fragment_ref, str) or fragment_ref.count("/") != 1:
            raise CompositionError(
                "CompositionUnknownFragment",
                "fragment references must use '<import-alias>/<metadata.name>'",
                path=f"$.spec.composition.slots.{slot_name}.fill.fragment",
                source=document.logical_source,
            )
        alias, fragment_name = fragment_ref.split("/", 1)
        if (
            COMPOSITION_NAME_PATTERN.fullmatch(alias) is None
            or COMPOSITION_NAME_PATTERN.fullmatch(fragment_name) is None
        ):
            raise CompositionError(
                "CompositionUnknownFragment",
                "fragment aliases and names must match ^[A-Za-z][A-Za-z0-9_-]*$",
                path=f"$.spec.composition.slots.{slot_name}.fill.fragment",
                source=document.logical_source,
            )
        imported_source = imports.get(alias)
        if imported_source is None:
            raise CompositionError(
                "CompositionUnknownFragment",
                f"fragment import alias {alias!r} is not declared",
                path=f"$.spec.composition.slots.{slot_name}.fill.fragment",
                source=document.logical_source,
            )
        fragment_document = self._find_fragment(imported_source, fragment_name, document)
        slot_interface = slot.get("interface")
        fragment_spec = fragment_document.value.get("spec")
        if not isinstance(fragment_spec, dict):
            raise CompositionError(
                "CompositionInvalidWiring",
                "GraphFragment spec must be a mapping",
                source=fragment_document.logical_source,
            )
        fragment_interface = fragment_spec.get("interface")
        _validate_interface(slot_interface, document.logical_source, slot_name)
        _validate_interface(
            fragment_interface,
            fragment_document.logical_source,
            fragment_name,
        )
        if slot_interface != fragment_interface:
            raise CompositionError(
                "CompositionInterfaceMismatch",
                f"slot {slot_name!r} and fragment {fragment_ref!r} interfaces must match exactly",
                path=f"$.spec.composition.slots.{slot_name}.interface",
                source=document.logical_source,
            )
        return fragment_ref, fragment_document, slot_interface, fragment_spec

    def _alias_sources(self, document: _SourceDocument) -> dict[str, Path]:
        composition = _composition_mapping(document)
        assert composition is not None
        imports = composition.get("imports", {})
        if not isinstance(imports, dict):
            return {}
        aliases: dict[str, Path] = {}
        for alias, entry in imports.items():
            if isinstance(alias, str) and isinstance(entry, dict) and isinstance(entry.get("path"), str):
                aliases[alias] = self._resolve_import(document.source, entry["path"], alias)
        return aliases

    def _find_fragment(
        self,
        source: Path,
        name: str,
        owner: _SourceDocument,
    ) -> _SourceDocument:
        matches = [
            document
            for document in self._documents_by_source[source]
            if document.value.get("kind") == "GraphFragment"
            and isinstance(document.value.get("metadata"), dict)
            and document.value["metadata"].get("name") == name
        ]
        if len(matches) != 1:
            raise CompositionError(
                "CompositionUnknownFragment" if not matches else "CompositionDuplicateIdentity",
                f"fragment {name!r} must identify exactly one GraphFragment",
                source=owner.logical_source,
            )
        return matches[0]

    def _splice_fragment(
        self,
        *,
        graph_name: str,
        graph_source: str,
        node_name: str,
        fragment_ref: str,
        fragment_source: str,
        interface: object,
        nodes: dict[str, Any],
        edges: list[Any],
        fragment_nodes: dict[str, Any],
        fragment_edges: list[Any],
    ) -> None:
        assert isinstance(interface, dict)
        input_ports = set(interface.get("inputs", {}))
        output_ports = set(interface.get("outputs", {}))
        incoming: dict[str, list[dict[str, str]]] = {port: [] for port in input_ports}
        outgoing: dict[str, list[dict[str, str]]] = {port: [] for port in output_ports}
        unrelated: list[dict[str, str]] = []

        for raw_edge in edges:
            edge = _edge(raw_edge, graph_source)
            target_owner, target_port = _endpoint(edge["to"])
            source_owner, source_port = _endpoint(edge["from"])
            touches_instance = False
            if target_owner == node_name:
                touches_instance = True
                if target_port not in incoming:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        f"slot instance {node_name!r} has unknown input port {target_port!r}",
                        source=graph_source,
                    )
                incoming[target_port].append(edge)
            if source_owner == node_name:
                touches_instance = True
                if source_port not in outgoing:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        f"slot instance {node_name!r} has unknown output port {source_port!r}",
                        source=graph_source,
                    )
                outgoing[source_port].append(edge)
            if not touches_instance:
                unrelated.append(edge)
        for port, port_edges in incoming.items():
            if len(port_edges) != 1:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    f"slot input {node_name}.{port} requires exactly one inbound edge",
                    source=graph_source,
                )

        fragment_inputs: dict[str, list[dict[str, str]]] = {port: [] for port in input_ports}
        fragment_outputs: dict[str, list[dict[str, str]]] = {port: [] for port in output_ports}
        internal_edges: list[dict[str, str]] = []
        inner_names = set(fragment_nodes)
        for raw_edge in fragment_edges:
            edge = _edge(raw_edge, fragment_source)
            source_owner, source_port = _endpoint(edge["from"])
            target_owner, target_port = _endpoint(edge["to"])
            if source_owner == "$input":
                if source_port not in fragment_inputs:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        f"fragment uses undeclared input port {source_port!r}",
                        source=fragment_source,
                    )
                if target_owner not in inner_names:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        "fragment inputs must target internal nodes",
                        source=fragment_source,
                    )
                fragment_inputs[source_port].append(edge)
            elif target_owner == "$output":
                if target_port not in fragment_outputs:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        f"fragment uses undeclared output port {target_port!r}",
                        source=fragment_source,
                    )
                if source_owner not in inner_names:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        "fragment outputs must originate from internal nodes",
                        source=fragment_source,
                    )
                fragment_outputs[target_port].append(edge)
            elif source_owner in {"$output"} or target_owner in {"$input"}:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    "fragment interface edges have an invalid direction",
                    source=fragment_source,
                )
            else:
                if source_owner not in inner_names or target_owner not in inner_names:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        "fragment ordinary endpoints must name internal nodes",
                        source=fragment_source,
                    )
                internal_edges.append(
                    {
                        "from": _prefix_endpoint(edge["from"], node_name, inner_names),
                        "to": _prefix_endpoint(edge["to"], node_name, inner_names),
                    }
                )
        for port, port_edges in fragment_inputs.items():
            if not port_edges:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    f"fragment input {port!r} is not consumed",
                    source=fragment_source,
                )
        for port, port_edges in fragment_outputs.items():
            if len(port_edges) != 1:
                raise CompositionError(
                    "CompositionInvalidWiring",
                    f"fragment output {port!r} requires exactly one producer",
                    source=fragment_source,
                )

        expanded_nodes: dict[str, Any] = {}
        for inner_name, raw_inner_node in fragment_nodes.items():
            expanded_name = f"{node_name}__{inner_name}"
            if expanded_name in nodes or expanded_name in expanded_nodes:
                raise CompositionError(
                    "CompositionNodeCollision",
                    f"expanded node {expanded_name!r} collides with an existing node",
                    source=graph_source,
                )
            inner_node = deepcopy(raw_inner_node)
            if isinstance(inner_node, dict) and isinstance(inner_node.get("when"), str):
                when_owner, _when_port = _endpoint(inner_node["when"])
                if when_owner not in inner_names:
                    raise CompositionError(
                        "CompositionInvalidWiring",
                        "fragment when references must name internal nodes",
                        source=fragment_source,
                    )
                inner_node["when"] = _prefix_endpoint(inner_node["when"], node_name, inner_names)
            expanded_nodes[expanded_name] = inner_node

        expanded_edges = list(unrelated)
        expanded_edges.extend(internal_edges)
        for port, port_edges in fragment_inputs.items():
            external_source = incoming[port][0]["from"]
            external_owner, external_port = _endpoint(external_source)
            if external_owner == node_name:
                external_source = _prefix_endpoint(
                    fragment_outputs[external_port][0]["from"],
                    node_name,
                    inner_names,
                )
            for edge in port_edges:
                expanded_edges.append(
                    {
                        "from": external_source,
                        "to": _prefix_endpoint(edge["to"], node_name, inner_names),
                    }
                )
        for port, port_edges in outgoing.items():
            fragment_source_endpoint = _prefix_endpoint(
                fragment_outputs[port][0]["from"],
                node_name,
                inner_names,
            )
            for edge in port_edges:
                target_owner, _target_port = _endpoint(edge["to"])
                if target_owner == node_name:
                    continue
                expanded_edges.append({"from": fragment_source_endpoint, "to": edge["to"]})

        del nodes[node_name]
        nodes.update(expanded_nodes)
        edges[:] = expanded_edges
        self._instances.append(
            CompositionInstance(
                graph=graph_name,
                node=node_name,
                fragment=fragment_ref,
                source=fragment_source,
            )
        )

    def _reject_duplicate_resources(self, documents: list[dict[str, Any]]) -> None:
        seen: set[tuple[str, str, str]] = set()
        for document in documents:
            metadata = document.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
                continue
            identity = (
                str(document.get("apiVersion")),
                str(document.get("kind")),
                metadata["name"],
            )
            if identity in seen:
                raise CompositionError(
                    "CompositionDuplicateIdentity",
                    f"duplicate emitted resource identity {identity!r}",
                )
            seen.add(identity)

    def _logical_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.name


def _composition_mapping(document: _SourceDocument) -> dict[str, Any] | None:
    spec = document.value.get("spec")
    if not isinstance(spec, dict) or "composition" not in spec:
        return None
    composition = spec.get("composition")
    if not isinstance(composition, dict):
        raise CompositionError(
            "CompositionUnsupportedVersion",
            "spec.composition must be a mapping",
            path="$.spec.composition",
            source=document.logical_source,
        )
    if composition.get("apiVersion") != COMPOSITION_API_VERSION:
        raise CompositionError(
            "CompositionUnsupportedVersion",
            f"composition apiVersion must be {COMPOSITION_API_VERSION!r}",
            path="$.spec.composition.apiVersion",
            source=document.logical_source,
        )
    unknown = set(composition) - {"apiVersion", "imports", "slots"}
    if unknown:
        raise CompositionError(
            "CompositionUnsupportedVersion",
            f"unsupported composition field {sorted(unknown)[0]!r}",
            path="$.spec.composition",
            source=document.logical_source,
        )
    return composition


def _validate_fragment_envelope(document: _SourceDocument) -> None:
    value = document.value
    if set(value) != {"apiVersion", "kind", "metadata", "spec"}:
        raise CompositionError(
            "CompositionUnsupportedKind",
            "GraphFragment documents require exactly apiVersion, kind, metadata, and spec",
            source=document.logical_source,
        )
    if value.get("apiVersion") != COMPOSITION_API_VERSION:
        raise CompositionError(
            "CompositionUnsupportedVersion",
            f"GraphFragment apiVersion must be {COMPOSITION_API_VERSION!r}",
            path="$.apiVersion",
            source=document.logical_source,
        )
    metadata = value.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or COMPOSITION_NAME_PATTERN.fullmatch(name) is None:
        raise CompositionError(
            "CompositionInvalidWiring",
            "GraphFragment metadata.name must match ^[A-Za-z][A-Za-z0-9_-]*$",
            path="$.metadata.name",
            source=document.logical_source,
        )
    spec = value.get("spec")
    if not isinstance(spec, dict) or not {"interface", "nodes"}.issubset(spec):
        raise CompositionError(
            "CompositionInvalidWiring",
            "GraphFragment spec requires interface and nodes",
            path="$.spec",
            source=document.logical_source,
        )
    unknown = set(spec) - {"interface", "nodes", "edges"}
    if unknown:
        raise CompositionError(
            "CompositionUnsupportedKind",
            f"unsupported GraphFragment field {sorted(unknown)[0]!r}",
            path="$.spec",
            source=document.logical_source,
        )
    _validate_interface(spec["interface"], document.logical_source, name)
    nodes = spec.get("nodes")
    if not isinstance(nodes, dict):
        raise CompositionError(
            "CompositionInvalidWiring",
            "GraphFragment spec.nodes must be a mapping",
            path="$.spec.nodes",
            source=document.logical_source,
        )
    for node_name, node in nodes.items():
        if (
            not isinstance(node_name, str)
            or COMPOSITION_NAME_PATTERN.fullmatch(node_name) is None
            or not isinstance(node, dict)
            or "slot" in node
            or not isinstance(node.get("block"), str)
            or "@" not in node["block"]
            or node["block"].endswith("@")
        ):
            raise CompositionError(
                "CompositionInvalidWiring",
                "GraphFragment nodes require safe names and ordinary block declarations",
                path=f"$.spec.nodes.{node_name}",
                source=document.logical_source,
            )
    edges = spec.get("edges", [])
    if not isinstance(edges, list):
        raise CompositionError(
            "CompositionInvalidWiring",
            "GraphFragment spec.edges must be a list",
            path="$.spec.edges",
            source=document.logical_source,
        )
    for edge in edges:
        _edge(edge, document.logical_source)


def _validate_imported_binding(document: _SourceDocument) -> None:
    value = document.value
    metadata = value.get("metadata")
    if (
        value.get("apiVersion") != "graphblocks.ai/v1alpha1"
        or not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
        or not isinstance(value.get("spec"), dict)
    ):
        raise CompositionError(
            "CompositionUnsupportedKind",
            "imported Binding documents require the v1alpha1 resource envelope",
            path=f"$[{document.index}]",
            source=document.logical_source,
        )


def _validate_interface(interface: object, source: str, owner: str) -> None:
    if not isinstance(interface, dict) or set(interface) != {"inputs", "outputs"}:
        raise CompositionError(
            "CompositionInterfaceMismatch",
            "composition interfaces require exactly inputs and outputs mappings",
            source=source,
        )
    for direction in ("inputs", "outputs"):
        ports = interface.get(direction)
        if not isinstance(ports, dict) or any(
            not isinstance(name, str) or not isinstance(schema, str)
            for name, schema in ports.items()
        ):
            raise CompositionError(
                "CompositionInterfaceMismatch",
                f"{owner!r} interface {direction} must map port names to schema IDs",
                source=source,
            )
        for port_name, schema_id in ports.items():
            if COMPOSITION_NAME_PATTERN.fullmatch(port_name) is None:
                raise CompositionError(
                    "CompositionInterfaceMismatch",
                    f"{owner!r} interface port names must be direct endpoint names",
                    source=source,
                )
            try:
                SchemaId.parse(schema_id)
            except SchemaIdError as error:
                raise CompositionError(
                    "CompositionInterfaceMismatch",
                    f"{owner!r} interface schema ID is invalid: {error}",
                    source=source,
                ) from error


def _metadata_name(document: dict[str, Any], source: _SourceDocument) -> str:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise CompositionError(
            "CompositionInvalidWiring",
            "composed Graph metadata.name must be a string",
            source=source.logical_source,
        )
    return metadata["name"]


def _edge(value: object, source: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"from", "to"}
        or not isinstance(value.get("from"), str)
        or not isinstance(value.get("to"), str)
    ):
        raise CompositionError(
            "CompositionInvalidWiring",
            "composition edges require exactly string from and to endpoints",
            source=source,
        )
    return {"from": value["from"], "to": value["to"]}


def _endpoint(value: str) -> tuple[str, str]:
    owner, separator, port = value.partition(".")
    if separator == "" or not owner or not port:
        raise CompositionError(
            "CompositionInvalidWiring",
            f"invalid edge endpoint {value!r}",
        )
    return owner, port


def _prefix_endpoint(value: str, instance: str, inner_names: set[str]) -> str:
    owner, separator, port = value.partition(".")
    if separator and owner in inner_names:
        return f"{instance}__{owner}.{port}"
    return value


def _resource_sort_key(document: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = document.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else ""
    return (
        str(document.get("apiVersion", "")),
        str(document.get("kind", "")),
        str(name),
        canonical_hash(document),
    )


def _reject_symlink_components(path: Path, *, owner: str) -> None:
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise CompositionError(
                "CompositionSymlinkRejected",
                f"{owner} must not traverse symbolic links",
            )


def _resolve_entry_and_root(path: str | Path, root: str | Path | None) -> tuple[Path, Path]:
    raw_entry = Path(path).absolute()
    raw_root = Path(root).absolute() if root is not None else raw_entry.parent
    _reject_symlink_components(raw_root, owner="composition root")
    _reject_symlink_components(raw_entry, owner="composition entry")
    try:
        resolved_root = raw_root.resolve(strict=True)
        resolved_entry = raw_entry.resolve(strict=True)
    except OSError as error:
        raise CompositionError(
            "CompositionInvalidImport",
            "composition entry and root must exist",
        ) from error
    if not resolved_root.is_dir():
        raise CompositionError(
            "CompositionInvalidImport",
            "composition root must be a directory",
        )
    try:
        relative_entry = raw_entry.relative_to(raw_root)
    except ValueError as error:
        raise CompositionError(
            "CompositionImportOutsideRoot",
            "composition entry must be within the composition root",
        ) from error
    current = raw_root
    for part in relative_entry.parts:
        current = current / part
        if current.is_symlink():
            raise CompositionError(
                "CompositionSymlinkRejected",
                "composition entry must not traverse symbolic links",
            )
    try:
        resolved_entry.relative_to(resolved_root)
    except ValueError as error:
        raise CompositionError(
            "CompositionImportOutsideRoot",
            "composition entry resolves outside the composition root",
        ) from error
    if not resolved_entry.is_file() or resolved_entry.suffix.lower() not in {".yaml", ".yml"}:
        raise CompositionError(
            "CompositionInvalidImport",
            "composition entry must be a regular .yaml or .yml file",
        )
    return resolved_entry, resolved_root


def compose_documents(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> CompositionResult:
    entry, composition_root = _resolve_entry_and_root(path, root)
    return _Composer(entry, composition_root).compose()


__all__ = [
    "COMPOSITION_API_VERSION",
    "CompositionError",
    "CompositionInstance",
    "CompositionReport",
    "CompositionResult",
    "CompositionSource",
    "compose_documents",
]
