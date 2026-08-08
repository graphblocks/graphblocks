from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from decimal import Decimal
import hashlib
from importlib.metadata import (
    PackageNotFoundError,
    distributions as installed_distributions,
    version as distribution_version,
)
import json
import os
from pathlib import Path
import platform as platform_module
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
import venv

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
import yaml

from graphblocks.canonical import (
    canonical_dumps_reference as canonical_dumps,
    canonical_hash_reference as canonical_hash,
    canonical_loads_reference as canonical_loads,
)
from graphblocks.conformance import ConformanceAuthorityMatrix
from graphblocks.loader import load_documents
from graphblocks.packages import build_wheel_matrix, load_package_catalog
from graphblocks.schema import SchemaId, SchemaManifest


ROOT = Path(__file__).resolve().parents[1]
CYCLONEDX_BOM_VERSION = "7.3.0"
PINNED_RUSTC_VERSION = "1.94.0"
PINNED_BUILD_TOOLS = {
    "pip": "25.1.1",
    "build": "1.5.1",
    "hatchling": "1.31.0",
    "maturin": "1.14.1",
}
SUPPORTED_PLATFORM_MATRIX = {
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.12"),
    ("windows-latest", "3.11"),
    ("windows-latest", "3.12"),
}
STABLE_CONFORMANCE_PROFILES = ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME")
NATIVE_BINDING_PROTOCOL_VERSION = 1
REQUIRED_NATIVE_BINDING_CAPABILITIES = (
    "canonical.json.v1",
    "compiler.graph.v1",
    "protocol.application.v1",
    "protocol.worker.v1",
    "runtime.local.v1",
    "schema.identity.v1",
    "schema.resource-migration.v1",
    "schema.resource-validation.v1",
)
NATIVE_CANONICAL_SMOKE_JSON = '{"a":1,"b":2}'
NATIVE_CANONICAL_SMOKE_HASH = (
    "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
)
NATIVE_SCHEMA_ID_SMOKE = {
    "canonical": "schemas/Message@4294967295",
    "majorVersion": 4_294_967_295,
    "name": "schemas/Message",
}
NATIVE_AUTHORITY_EVIDENCE_FORMAT_VERSION = 2
NATIVE_CANONICAL_AUTHORITY_SOURCES = (
    '{"b":2,"a":1}',
    '[null,true,false,0,-17,1.25,"snowman ☃"]',
    '{"nested":{"z":[3,2,1],"a":"é"}}',
)
NATIVE_SCHEMA_ID_AUTHORITY_SOURCES = (
    "schemas/Message@1",
    "schemas/Message@4294967295",
    "graphblocks.ai/PluginManifest@1",
)
NATIVE_RESOURCE_VALIDATION_CASES_PATH = ROOT / "tck" / "schema" / "resources.json"
NATIVE_RESOURCE_MIGRATION_CASES_PATH = ROOT / "tck" / "migration" / "cases.json"
STABLE_RUNTIME_API_SNAPSHOT_PATH = ROOT / "compatibility" / "stable-runtime-api.json"
STABLE_RUNTIME_SURFACE_PATH = ROOT / "compatibility" / "stable-runtime-surface.yaml"
STABLE_RUNTIME_TCK_CASES_PATH = ROOT / "tck" / "runtime" / "cases.json"
STABLE_RUNTIME_EXPORTS = (
    "native_extension_status",
    "require_native_extension",
    "run_stdlib_graph",
)
STABLE_RUNTIME_STATUS_FIELDS = (
    "available",
    "binding_crate",
    "binding_protocol_version",
    "binding_version",
    "capabilities",
    "error",
    "module",
)
STABLE_RUNTIME_SMOKE_RUN_ID = "installed-stable-runtime-api"
MAX_SDIST_MEMBER_COUNT = 100_000
MAX_SDIST_UNPACKED_SIZE = 512 * 1024 * 1024
WINDOWS_RESERVED_PATH_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
BASE_GRAPHBLOCKS_INSTALL_PROBE = (
    "import importlib, json, sys; "
    "from importlib.metadata import distributions, requires, version; "
    "from packaging.requirements import Requirement; "
    "from packaging.utils import canonicalize_name; "
    "import graphblocks; "
    "root_modules = sorted(name for name in sys.modules "
    "if name == 'graphblocks' or name.startswith('graphblocks.')); "
    "root_public_names = sorted(name for name in dir(graphblocks) "
    "if not name.startswith('_')); "
    "root_attributes = len(vars(graphblocks)); "
    "importlib.import_module('graphblocks.canonical'); "
    "canonical_modules = sorted(name for name in sys.modules "
    "if name == 'graphblocks' or name.startswith('graphblocks.')); "
    "stable_resolved = [name for name in graphblocks.__all__ "
    "if hasattr(graphblocks, name)]; "
    "graphblocks_distributions = sorted({canonicalize_name("
    "str(distribution.metadata['Name'])) for distribution in "
    "distributions() if canonicalize_name("
    "str(distribution.metadata['Name'])).startswith('graphblocks')}); "
    "print(json.dumps({"
    "'canonicalModules': canonical_modules, "
    "'distributionVersion': version('graphblocks'), "
    "'graphblocksDistributions': graphblocks_distributions, "
    "'requirements': sorted(str(Requirement(requirement)) "
    "for requirement in (requires('graphblocks') or ())), "
    "'rootAll': list(graphblocks.__all__), "
    "'rootAttributes': root_attributes, "
    "'rootModules': root_modules, "
    "'rootPublicNames': root_public_names, "
    "'stableResolved': stable_resolved"
    "}, sort_keys=True))"
)


def validate_base_graphblocks_install(
    payload: object,
    *,
    expected_version: str,
    expected_requirements: Sequence[str],
    expected_root_exports: Sequence[str],
    expected_root_modules: Sequence[str],
    expected_canonical_modules: Sequence[str],
    max_root_attributes: int,
) -> dict[str, object]:
    """Validate the isolated base-wheel dependency and import boundary."""

    required_fields = {
        "canonicalModules",
        "distributionVersion",
        "graphblocksDistributions",
        "requirements",
        "rootAll",
        "rootAttributes",
        "rootModules",
        "rootPublicNames",
        "stableResolved",
    }
    if not isinstance(payload, dict) or set(payload) != required_fields:
        raise RuntimeError("base graphblocks install probe has an invalid envelope")
    observed_requirements = payload["requirements"]
    if not isinstance(observed_requirements, list) or not all(
        isinstance(requirement, str) and requirement
        for requirement in observed_requirements
    ):
        raise RuntimeError(
            "base graphblocks install requirements must be exact PEP 508 strings"
        )
    try:
        expected_requirement_objects = {
            Requirement(requirement) for requirement in expected_requirements
        }
        observed_requirement_objects = {
            Requirement(requirement) for requirement in observed_requirements
        }
    except InvalidRequirement as error:
        raise RuntimeError(
            "base graphblocks install metadata contains an invalid requirement"
        ) from error
    if (
        len(observed_requirement_objects) != len(observed_requirements)
        or observed_requirement_objects != expected_requirement_objects
    ):
        raise RuntimeError(
            "base graphblocks install metadata requirements mismatch: "
            f"expected {sorted(map(str, expected_requirement_objects))!r}, "
            f"observed {sorted(map(str, observed_requirement_objects))!r}"
        )
    expected_ordered_exports = list(expected_root_exports)
    checks = {
        "distribution version": (
            payload["distributionVersion"],
            expected_version,
        ),
        "first-party distributions": (
            payload["graphblocksDistributions"],
            ["graphblocks"],
        ),
        "root __all__": (payload["rootAll"], expected_ordered_exports),
        "root public discovery": (
            payload["rootPublicNames"],
            sorted(expected_ordered_exports),
        ),
        "root modules": (payload["rootModules"], list(expected_root_modules)),
        "canonical modules": (
            payload["canonicalModules"],
            list(expected_canonical_modules),
        ),
        "stable symbol resolution": (
            payload["stableResolved"],
            expected_ordered_exports,
        ),
    }
    for label, (observed, expected) in checks.items():
        if observed != expected:
            raise RuntimeError(
                f"base graphblocks install {label} mismatch: "
                f"expected {expected!r}, observed {observed!r}"
            )
    root_attributes = payload["rootAttributes"]
    if (
        not isinstance(root_attributes, int)
        or isinstance(root_attributes, bool)
        or root_attributes > max_root_attributes
    ):
        raise RuntimeError(
            "base graphblocks install root attribute budget exceeded: "
            f"maximum {max_root_attributes}, observed {root_attributes!r}"
        )
    return dict(payload)


def _is_nonportable_windows_path_part(part: str) -> bool:
    stem = part.split(".", 1)[0].casefold()
    return (
        ":" in part
        or part.endswith((".", " "))
        or stem in WINDOWS_RESERVED_PATH_NAMES
    )


def _tool_command(executable: str | Sequence[str]) -> list[str]:
    command = [executable] if isinstance(executable, str) else list(executable)
    if not command or not all(isinstance(part, str) and part for part in command):
        raise RuntimeError("tool executable command must not be empty")
    return command


def _sanitized_process_output(output: str | None) -> str:
    normalized = " ".join(
        re.sub(r"[\x00-\x1f\x7f]+", " ", output or "").split()
    )
    if len(normalized) > 1_000:
        normalized = normalized[:997] + "..."
    return normalized or "<empty>"


def parse_rustc_identity(output: str) -> dict[str, str]:
    normalized = output.strip()
    match = re.fullmatch(r"rustc ([0-9]+\.[0-9]+\.[0-9]+)(?: .+)?", normalized)
    if match is None:
        raise RuntimeError("rustc --version returned an unrecognized identity")
    version = match.group(1)
    if version != PINNED_RUSTC_VERSION:
        raise RuntimeError(
            f"release builds require rustc=={PINNED_RUSTC_VERSION}, found {version!r}"
        )
    return {"version": version, "output": normalized}


def observe_rustc_identity(
    executable: str | Sequence[str] = "rustc",
) -> dict[str, str]:
    command = [*_tool_command(executable), "--version"]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"release builds require rustc=={PINNED_RUSTC_VERSION}; "
            f"rustc --version failed with exit code {error.returncode}; "
            f"stdout={_sanitized_process_output(error.stdout)!r}; "
            f"stderr={_sanitized_process_output(error.stderr)!r}"
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"release builds require rustc=={PINNED_RUSTC_VERSION}; "
            "rustc --version could not be executed: "
            f"{_sanitized_process_output(str(error))}"
        ) from error
    return parse_rustc_identity(completed.stdout)


def _load_native_authority_cases(
    path: Path,
    *,
    owner: str,
) -> tuple[dict[str, object], ...]:
    try:
        payload = canonical_loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError(f"cannot load {owner} authority cases") from error
    if type(payload) is not list or not payload:
        raise RuntimeError(f"{owner} authority cases must be a nonempty array")
    cases: list[dict[str, object]] = []
    names: set[str] = set()
    for index, case in enumerate(payload):
        if type(case) is not dict or set(case) != {"document", "expected", "name"}:
            raise RuntimeError(f"{owner} authority case {index} must be closed")
        name = case["name"]
        if type(name) is not str or not name or name in names:
            raise RuntimeError(f"{owner} authority case {index} has an invalid name")
        if type(case["expected"]) is not dict:
            raise RuntimeError(f"{owner} authority case {name!r} has invalid fields")
        names.add(name)
        cases.append(case)
    return tuple(cases)


def _expected_resource_validation_contract(
    case: Mapping[str, object],
) -> dict[str, object]:
    expected = case["expected"]
    if type(expected) is not dict or set(expected).difference({"errors", "valid"}):
        raise RuntimeError("resource validation authority expectation must be closed")
    valid = expected.get("valid")
    raw_errors = expected.get("errors", [])
    if type(valid) is not bool or type(raw_errors) is not list:
        raise RuntimeError("resource validation authority expectation has invalid fields")
    errors: list[dict[str, str]] = []
    error_fields = {"code", "keyword", "path"}
    for index, error in enumerate(raw_errors):
        if (
            type(error) is not dict
            or set(error) != error_fields
            or any(type(error[field]) is not str or not error[field] for field in error_fields)
        ):
            raise RuntimeError(
                f"resource validation authority error {index} must be closed"
            )
        errors.append({field: error[field] for field in sorted(error_fields)})
    if valid != (not errors):
        raise RuntimeError("resource validation authority validity is inconsistent")
    return {"errors": errors, "valid": valid}


def _expected_resource_migration_contract(
    case: Mapping[str, object],
) -> dict[str, object]:
    expected = case["expected"]
    if type(expected) is not dict:
        raise RuntimeError("resource migration authority expectation must be an object")
    if "document" in expected:
        if set(expected).difference({"document", "normalized"}):
            raise RuntimeError("resource migration authority success must be closed")
        document = expected["document"]
        if type(document) is not dict:
            raise RuntimeError(
                "resource migration authority success document must be an object"
            )
        return {"document": document, "ok": True}
    if set(expected) != {"error"} or type(expected["error"]) is not dict:
        raise RuntimeError("resource migration authority failure must be closed")
    error = expected["error"]
    if (
        set(error) != {"code", "path"}
        or any(type(error[field]) is not str or not error[field] for field in error)
    ):
        raise RuntimeError("resource migration authority error must be closed")
    return {
        "error": {"code": error["code"], "path": error["path"]},
        "ok": False,
    }


def _authority_case_evidence(
    case: Mapping[str, object],
    contract: Mapping[str, object],
) -> dict[str, object]:
    return {
        "name": case["name"],
        "public": canonical_loads(canonical_dumps(contract)),
        "reference": canonical_loads(canonical_dumps(contract)),
        "native": canonical_loads(canonical_dumps(contract)),
    }


def installed_native_authority_probe_expectations() -> dict[str, object]:
    resource_validation_cases = _load_native_authority_cases(
        NATIVE_RESOURCE_VALIDATION_CASES_PATH,
        owner="resource validation",
    )
    resource_migration_cases = _load_native_authority_cases(
        NATIVE_RESOURCE_MIGRATION_CASES_PATH,
        owner="resource migration",
    )
    canonical_cases: list[dict[str, object]] = []
    for source in NATIVE_CANONICAL_AUTHORITY_SOURCES:
        value = canonical_loads(source)
        result = {
            "json": canonical_dumps(value),
            "hash": canonical_hash(value),
        }
        canonical_cases.append(
            {
                "source": source,
                "public": dict(result),
                "reference": dict(result),
                "native": dict(result),
            }
        )
    schema_id_cases: list[dict[str, object]] = []
    for source in NATIVE_SCHEMA_ID_AUTHORITY_SOURCES:
        schema_id = SchemaId.parse_reference(source)
        identity = {
            "canonical": schema_id.as_str(),
            "majorVersion": schema_id.major_version,
            "name": schema_id.name,
        }
        schema_id_cases.append(
            {
                "source": source,
                "public": dict(identity),
                "reference": dict(identity),
                "native": dict(identity),
            }
        )
    return {
        "formatVersion": NATIVE_AUTHORITY_EVIDENCE_FORMAT_VERSION,
        "corpusDigest": canonical_hash(
            {
                "canonicalSources": list(NATIVE_CANONICAL_AUTHORITY_SOURCES),
                "schemaIds": list(NATIVE_SCHEMA_ID_AUTHORITY_SOURCES),
                "resourceValidationCases": list(resource_validation_cases),
                "resourceMigrationCases": list(resource_migration_cases),
            }
        ),
        "canonicalCases": canonical_cases,
        "schemaIdCases": schema_id_cases,
        "resourceValidationCases": [
            _authority_case_evidence(
                case,
                _expected_resource_validation_contract(case),
            )
            for case in resource_validation_cases
        ],
        "resourceMigrationCases": [
            _authority_case_evidence(
                case,
                _expected_resource_migration_contract(case),
            )
            for case in resource_migration_cases
        ],
    }


def stable_runtime_api_snapshot() -> dict[str, object]:
    try:
        snapshot = json.loads(
            STABLE_RUNTIME_API_SNAPSHOT_PATH.read_text(encoding="utf-8")
        )
        policy = yaml.safe_load(
            STABLE_RUNTIME_SURFACE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise RuntimeError("stable runtime API compatibility data is invalid") from error
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "authority",
        "exportPolicy",
        "implicitReferenceFallback",
        "nativeExtensionStatusFields",
        "profile",
        "readiness",
        "requiredCapability",
        "resultContract",
        "snapshotVersion",
        "stableExports",
        "symbols",
        "targetRelease",
    }:
        raise RuntimeError("stable runtime API snapshot has an invalid envelope")
    if not isinstance(policy, dict) or set(policy) != {
        "authority",
        "description",
        "exportPolicy",
        "implicitReferenceFallback",
        "previewHelpers",
        "profile",
        "readiness",
        "requiredCapability",
        "resultContract",
        "snapshotVersion",
        "symbols",
        "targetRelease",
    }:
        raise RuntimeError("stable runtime API surface has an invalid envelope")
    expected_metadata = {
        "authority": "rust",
        "exportPolicy": "explicit-stable-allowlist-unlisted-public-exports-preview",
        "implicitReferenceFallback": False,
        "profile": "GB-C1-LOCAL-RUNTIME",
        "readiness": "candidate",
        "requiredCapability": "runtime.local.v1",
        "resultContract": {
            "fields": ["runId", "graphHash", "status", "outputs", "journal"],
            "excludedPreviewFields": ["checkpoint", "deploymentProvenance"],
        },
        "snapshotVersion": 1,
        "targetRelease": "1.0",
    }
    for field, expected in expected_metadata.items():
        if snapshot.get(field) != expected or policy.get(field) != expected:
            raise RuntimeError(f"stable runtime API {field} does not match policy")
    expected_paths = [f"graphblocks_runtime.{name}" for name in STABLE_RUNTIME_EXPORTS]
    policy_symbols = policy["symbols"]
    snapshot_symbols = snapshot["symbols"]
    if (
        not isinstance(policy_symbols, list)
        or not isinstance(snapshot_symbols, list)
        or [
            entry.get("path") if isinstance(entry, dict) else None
            for entry in policy_symbols
        ]
        != expected_paths
        or [
            entry.get("path") if isinstance(entry, dict) else None
            for entry in snapshot_symbols
        ]
        != expected_paths
    ):
        raise RuntimeError("stable runtime API symbols do not match policy")
    for symbol in snapshot_symbols:
        if not isinstance(symbol, dict) or set(symbol) != {
            "kind",
            "path",
            "profile",
            "signature",
            "typeReferences",
        }:
            raise RuntimeError("stable runtime API snapshot symbol is invalid")
        if (
            symbol["kind"] != "function"
            or symbol["profile"] != "GB-C1-LOCAL-RUNTIME"
            or not isinstance(symbol["signature"], str)
            or not symbol["signature"]
            or symbol["typeReferences"] != []
        ):
            raise RuntimeError("stable runtime API snapshot symbol is invalid")
    if snapshot["stableExports"] != list(STABLE_RUNTIME_EXPORTS):
        raise RuntimeError("stable runtime API exports do not match policy")
    if snapshot["nativeExtensionStatusFields"] != list(
        STABLE_RUNTIME_STATUS_FIELDS
    ):
        raise RuntimeError("stable runtime API status fields do not match policy")
    preview_helpers = policy["previewHelpers"]
    if not isinstance(preview_helpers, list) or not all(
        isinstance(path, str) and path.startswith("graphblocks_runtime.")
        for path in preview_helpers
    ):
        raise RuntimeError("stable runtime API preview helper policy is invalid")
    if {
        path.rsplit(".", 1)[1] for path in preview_helpers
    }.intersection(STABLE_RUNTIME_EXPORTS):
        raise RuntimeError("stable runtime API preview helper was promoted")
    return snapshot


def _stable_runtime_smoke_case() -> dict[str, object]:
    try:
        cases = json.loads(STABLE_RUNTIME_TCK_CASES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("stable runtime smoke fixture is invalid") from error
    if not isinstance(cases, list):
        raise RuntimeError("stable runtime smoke fixture must contain cases")
    case = next(
        (
            value
            for value in cases
            if isinstance(value, dict) and value.get("name") == "prompt_render_output"
        ),
        None,
    )
    if (
        not isinstance(case, dict)
        or not isinstance(case.get("document"), dict)
        or not isinstance(case.get("inputs"), dict)
        or not isinstance(case.get("expected"), dict)
    ):
        raise RuntimeError("stable runtime smoke case is invalid")
    return case


def stable_runtime_smoke_expectation() -> dict[str, object]:
    expected = _stable_runtime_smoke_case()["expected"]
    if not isinstance(expected, dict):
        raise RuntimeError("stable runtime smoke expectation is invalid")
    status = expected.get("status")
    outputs = expected.get("outputs")
    terminal_kind = expected.get("terminal_kind")
    if (
        not isinstance(status, str)
        or not isinstance(outputs, dict)
        or not isinstance(terminal_kind, str)
    ):
        raise RuntimeError("stable runtime smoke expectation is invalid")
    return {
        "runId": STABLE_RUNTIME_SMOKE_RUN_ID,
        "status": status,
        "outputs": outputs,
        "terminalKind": terminal_kind,
    }


def installed_native_authority_probe_source() -> str:
    resource_validation_cases = _load_native_authority_cases(
        NATIVE_RESOURCE_VALIDATION_CASES_PATH,
        owner="resource validation",
    )
    resource_migration_cases = _load_native_authority_cases(
        NATIVE_RESOURCE_MIGRATION_CASES_PATH,
        owner="resource migration",
    )
    resource_validation_json = json.dumps(
        resource_validation_cases,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    resource_migration_json = json.dumps(
        resource_migration_cases,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    runtime_api_json = json.dumps(
        stable_runtime_api_snapshot(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    runtime_smoke_case = _stable_runtime_smoke_case()
    runtime_smoke_graph_json = json.dumps(
        runtime_smoke_case["document"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    runtime_smoke_inputs_json = json.dumps(
        runtime_smoke_case["inputs"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "\n".join(
        (
            "from copy import deepcopy",
            "import inspect",
            "import json",
            "from importlib.metadata import version",
            "import graphblocks_runtime",
            (
                "from graphblocks.canonical import canonical_dumps, "
                "canonical_dumps_reference, canonical_hash, "
                "canonical_hash_reference, canonical_loads, "
                "canonical_loads_reference"
            ),
            (
                "from graphblocks.migration import MigrationError, "
                "migrate_document, migrate_document_reference"
            ),
            (
                "from graphblocks.schema import SchemaId, resource_schema_errors, "
                "resource_schema_errors_reference"
            ),
            f"canonical_sources = {NATIVE_CANONICAL_AUTHORITY_SOURCES!r}",
            f"schema_id_sources = {NATIVE_SCHEMA_ID_AUTHORITY_SOURCES!r}",
            f"resource_validation_cases = json.loads({resource_validation_json!r})",
            f"resource_migration_cases = json.loads({resource_migration_json!r})",
            f"stable_runtime_api = json.loads({runtime_api_json!r})",
            f"stable_runtime_graph = json.loads({runtime_smoke_graph_json!r})",
            f"stable_runtime_inputs = json.loads({runtime_smoke_inputs_json!r})",
            "def validation_contract(errors):",
            "    return {'errors': [{'code': error.code, 'keyword': error.keyword, 'path': error.path} for error in errors], 'valid': not errors}",
            "def native_validation_contract(errors):",
            "    return {'errors': [{'code': error['code'], 'keyword': error['keyword'], 'path': error['path']} for error in errors], 'valid': not errors}",
            "def migration_contract(function, document):",
            "    try:",
            "        migrated = function(deepcopy(document))",
            "    except MigrationError as error:",
            "        return {'error': {'code': error.code, 'path': error.path}, 'ok': False}",
            "    return {'document': migrated, 'ok': True}",
            "def native_migration_contract(document):",
            "    result = graphblocks_runtime.migrate_resource(deepcopy(document))",
            "    if result['ok']:",
            "        return {'document': result['document'], 'ok': True}",
            "    return {'error': {'code': result['error']['code'], 'path': result['error']['path']}, 'ok': False}",
            "canonical_cases = []",
            "for source in canonical_sources:",
            "    public_value = canonical_loads(source)",
            "    reference_value = canonical_loads_reference(source)",
            "    canonical_cases.append({'source': source, 'public': {'json': canonical_dumps(public_value), 'hash': canonical_hash(public_value)}, 'reference': {'json': canonical_dumps_reference(reference_value), 'hash': canonical_hash_reference(reference_value)}, 'native': {'json': graphblocks_runtime.canonicalize_json(source), 'hash': graphblocks_runtime.canonical_hash_json(source)}})",
            "schema_id_cases = []",
            "for source in schema_id_sources:",
            "    public = SchemaId.parse(source)",
            "    reference = SchemaId.parse_reference(source)",
            "    schema_id_cases.append({'source': source, 'public': {'canonical': public.as_str(), 'majorVersion': public.major_version, 'name': public.name}, 'reference': {'canonical': reference.as_str(), 'majorVersion': reference.major_version, 'name': reference.name}, 'native': graphblocks_runtime.parse_schema_id(source)})",
            "resource_validation_evidence = []",
            "for case in resource_validation_cases:",
            "    document = case['document']",
            "    resource_validation_evidence.append({'name': case['name'], 'public': validation_contract(resource_schema_errors(deepcopy(document))), 'reference': validation_contract(resource_schema_errors_reference(deepcopy(document))), 'native': native_validation_contract(graphblocks_runtime.resource_schema_errors(deepcopy(document)))})",
            "resource_migration_evidence = []",
            "for case in resource_migration_cases:",
            "    document = case['document']",
            "    resource_migration_evidence.append({'name': case['name'], 'public': migration_contract(migrate_document, document), 'reference': migration_contract(migrate_document_reference, document), 'native': native_migration_contract(document)})",
            "corpus = {'canonicalSources': list(canonical_sources), 'schemaIds': list(schema_id_sources), 'resourceValidationCases': resource_validation_cases, 'resourceMigrationCases': resource_migration_cases}",
            "public_facade_evidence = {'formatVersion': 2, 'corpusDigest': canonical_hash_reference(corpus), 'canonicalCases': canonical_cases, 'schemaIdCases': schema_id_cases, 'resourceValidationCases': resource_validation_evidence, 'resourceMigrationCases': resource_migration_evidence}",
            "declared_stable_exports = stable_runtime_api['stableExports']",
            "observed_stable_symbols = []",
            "for symbol in stable_runtime_api['symbols']:",
            "    name = symbol['path'].rsplit('.', 1)[1]",
            "    value = getattr(graphblocks_runtime, name)",
            "    observed_symbol = dict(symbol)",
            "    observed_symbol['kind'] = 'function' if inspect.isfunction(value) else 'callable'",
            "    observed_symbol['signature'] = str(inspect.signature(value))",
            "    observed_stable_symbols.append(observed_symbol)",
            "stable_runtime_api['symbols'] = observed_stable_symbols",
            "stable_runtime_api['stableExports'] = [name for name in graphblocks_runtime.__all__ if name in declared_stable_exports]",
            "native_status = graphblocks_runtime.native_extension_status()",
            "stable_runtime_api['nativeExtensionStatusFields'] = sorted(native_status)",
            "graphblocks_runtime.require_native_extension()",
            f"stable_runtime_result = graphblocks_runtime.run_stdlib_graph(stable_runtime_graph, stable_runtime_inputs, run_id={STABLE_RUNTIME_SMOKE_RUN_ID!r})",
            "stable_runtime_terminal_kind = next((record.get('kind') for record in reversed(stable_runtime_result['journal']) if record.get('terminal') is True), None)",
            "stable_runtime_smoke = {'runId': stable_runtime_result['runId'], 'status': stable_runtime_result['status'], 'outputs': stable_runtime_result['outputs'], 'terminalKind': stable_runtime_terminal_kind}",
            "payload = {'canonicalSmoke': {'hash': graphblocks_runtime.canonical_hash_json('{\"b\":2,\"a\":1}'), 'json': graphblocks_runtime.canonicalize_json('{\"b\":2,\"a\":1}')}, 'distributionVersion': version('graphblocks-runtime'), 'publicFacadeEvidence': public_facade_evidence, 'schemaIdSmoke': graphblocks_runtime.parse_schema_id('schemas/Message@4294967295'), 'stableRuntimeApi': stable_runtime_api, 'stableRuntimeSmoke': stable_runtime_smoke, 'status': native_status}",
            "print(json.dumps(payload, sort_keys=True))",
        )
    )


def _validate_installed_native_binding(
    payload: object,
    *,
    expected_distribution_version: str,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "canonicalSmoke",
        "distributionVersion",
        "publicFacadeEvidence",
        "schemaIdSmoke",
        "stableRuntimeApi",
        "stableRuntimeSmoke",
        "status",
    }:
        raise RuntimeError(
            "installed native binding handshake has an invalid envelope"
        )
    if payload["distributionVersion"] != expected_distribution_version:
        raise RuntimeError(
            "installed native binding distribution version does not match "
            "the built graphblocks-runtime wheel"
        )
    status = payload["status"]
    expected_status_fields = {
        "available",
        "binding_crate",
        "binding_version",
        "binding_protocol_version",
        "capabilities",
        "module",
        "error",
    }
    if not isinstance(status, dict) or set(status) != expected_status_fields:
        raise RuntimeError("installed native binding status has invalid fields")
    if status["available"] is not True or status["error"] is not None:
        raise RuntimeError("installed native binding handshake is unavailable")
    if status["binding_crate"] != "graphblocks-python":
        raise RuntimeError(
            "installed native binding names an unexpected implementation"
        )
    if status["binding_version"] != expected_distribution_version:
        raise RuntimeError(
            "installed native binding implementation version does not match "
            "the graphblocks-runtime distribution"
        )
    protocol_version = status["binding_protocol_version"]
    if (
        type(protocol_version) is not int
        or protocol_version != NATIVE_BINDING_PROTOCOL_VERSION
    ):
        raise RuntimeError(
            "installed native binding reports an unsupported protocol version"
        )
    capabilities = status["capabilities"]
    if (
        not isinstance(capabilities, list)
        or not 1 <= len(capabilities) <= 64
        or not all(
            isinstance(capability, str)
            and capability
            and capability == capability.strip()
            and len(capability) <= 128
            and all(
                character.isascii()
                and (character.isalnum() or character in "._-")
                for character in capability
            )
            for capability in capabilities
        )
    ):
        raise RuntimeError(
            "installed native binding reports invalid capabilities"
        )
    if capabilities != sorted(set(capabilities)):
        raise RuntimeError(
            "installed native binding reports invalid capabilities"
        )
    missing_capabilities = sorted(
        set(REQUIRED_NATIVE_BINDING_CAPABILITIES).difference(capabilities)
    )
    if missing_capabilities:
        raise RuntimeError(
            "installed native binding is missing required capabilities: "
            + ", ".join(missing_capabilities)
        )
    if status["module"] != "graphblocks_runtime._native":
        raise RuntimeError(
            "installed native binding reports an unexpected extension module"
        )
    canonical_smoke = payload["canonicalSmoke"]
    if canonical_smoke != {
        "hash": NATIVE_CANONICAL_SMOKE_HASH,
        "json": NATIVE_CANONICAL_SMOKE_JSON,
    }:
        raise RuntimeError(
            "installed native binding canonical smoke does not match"
        )
    if payload["schemaIdSmoke"] != NATIVE_SCHEMA_ID_SMOKE:
        raise RuntimeError(
            "installed native binding schema id smoke does not match"
        )
    if payload["publicFacadeEvidence"] != (
        installed_native_authority_probe_expectations()
    ):
        raise RuntimeError(
            "installed public/native authority facade evidence does not match"
        )
    if payload["stableRuntimeApi"] != stable_runtime_api_snapshot():
        raise RuntimeError(
            "installed stable runtime API snapshot does not match"
        )
    if payload["stableRuntimeSmoke"] != stable_runtime_smoke_expectation():
        raise RuntimeError(
            "installed stable runtime API smoke does not match"
        )
    return dict(status)


def validate_installed_native_authority_evidence(
    payload: object,
    *,
    expected_runtime_artifact: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "probe",
        "runtimeArtifact",
    }:
        raise RuntimeError(
            "installed native authority evidence has an invalid envelope"
        )
    if payload["runtimeArtifact"] != dict(expected_runtime_artifact):
        raise RuntimeError(
            "installed native authority evidence names another runtime artifact"
        )
    runtime_version = expected_runtime_artifact.get("version")
    if not isinstance(runtime_version, str) or not runtime_version:
        raise RuntimeError(
            "installed native authority evidence has no runtime version"
        )
    _validate_installed_native_binding(
        payload["probe"],
        expected_distribution_version=runtime_version,
    )
    return dict(payload)


def _require_canonical_sha256(value: object, *, owner: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise RuntimeError(f"{owner} is not a canonical sha256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RuntimeError(f"{owner} is not a canonical sha256 digest")
    return value


def _write_utf8_lf(path: Path, value: str) -> None:
    path.write_bytes(value.encode("utf-8"))


def _acceptance_expectations(
    manifest_path: Path,
    *,
    root: Path,
) -> dict[str, object]:
    try:
        document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise RuntimeError("checked-in acceptance manifest is invalid") from error
    if not isinstance(document, dict) or document.get("kind") != "AcceptanceApplicationSet":
        raise RuntimeError("checked-in acceptance manifest has an invalid resource kind")
    spec = document.get("spec")
    applications = spec.get("applications") if isinstance(spec, dict) else None
    if not isinstance(applications, list) or not applications:
        raise RuntimeError("checked-in acceptance manifest contains no applications")

    contracts: list[dict[str, object]] = []
    expected: dict[str, object] = {}
    for raw_application in applications:
        if not isinstance(raw_application, dict):
            raise RuntimeError("checked-in acceptance manifest contains an invalid application")
        application_id = raw_application.get("id")
        profiles = raw_application.get("profiles")
        scenario_path = raw_application.get("scenarioPath")
        gates = raw_application.get("gates", [])
        if (
            not isinstance(application_id, str)
            or not application_id
            or application_id in expected
            or not isinstance(profiles, list)
            or not profiles
            or not all(isinstance(profile, str) and profile for profile in profiles)
            or not isinstance(scenario_path, str)
            or not scenario_path
            or not isinstance(gates, list)
            or not gates
            or not all(isinstance(gate, str) and gate for gate in gates)
        ):
            raise RuntimeError("checked-in acceptance manifest contains an invalid application")
        contract = {
            "application_id": application_id,
            "profiles": list(profiles),
            "scenario_path": scenario_path,
            "gates": list(gates),
            "description": str(raw_application.get("description", "")),
            "allow_unknown_blocks": raw_application.get("allowUnknownBlocks", False),
        }
        if not isinstance(contract["allow_unknown_blocks"], bool):
            raise RuntimeError("checked-in acceptance manifest contains an invalid application")
        try:
            scenario_digest = canonical_hash(load_documents(root / scenario_path))
        except (OSError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"checked-in acceptance scenario is invalid: {scenario_path}"
            ) from error
        contracts.append(contract)
        expected[application_id] = {
            "application_digest": canonical_hash(contract),
            "scenario_path": scenario_path,
            "scenario_digest": scenario_digest,
            "gates": tuple(gates),
        }
    contracts.sort(key=lambda application: str(application["application_id"]))
    return {
        "manifest_digest": canonical_hash({"applications": contracts}),
        "applications": expected,
    }


def _case_id(raw_case: object, *, owner: str) -> str:
    if not isinstance(raw_case, Mapping):
        raise RuntimeError(f"{owner} contains a non-object case")
    value = next(
        (
            raw_case[key]
            for key in ("name", "case_id", "caseId")
            if key in raw_case
        ),
        None,
    )
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{owner} contains a case without an id")
    return value


def _load_json_array(path: Path, *, owner: str) -> list[object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{owner} is invalid") from error
    if not isinstance(value, list):
        raise RuntimeError(f"{owner} must be a JSON array")
    return value


def _tck_expectations(
    root: Path,
    *,
    implementation: str = "graphblocks-python",
    implementation_version: str | None = None,
) -> dict[str, object]:
    source_tck_root = root / "tck"
    tck_root = (
        root
        / "packages"
        / "graphblocks-testing"
        / "src"
        / "graphblocks_testing"
        / "fixtures"
        / "tck"
    )
    if implementation_version is None:
        try:
            project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
                "project"
            ]
            implementation_version = str(project["version"])
        except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise RuntimeError("checked-in implementation version is invalid") from error
    try:
        native_compiler_project = tomllib.loads(
            (
                root
                / "packages"
                / "graphblocks-runtime"
                / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )["project"]
        native_compiler_version = str(native_compiler_project["version"])
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise RuntimeError("checked-in native compiler version is invalid") from error
    suites: dict[str, dict[str, object]] = {}
    contracts: list[dict[str, object]] = []
    for cases_path in sorted(tck_root.glob("*/cases.json"), key=lambda path: path.parent.name):
        suite = cases_path.parent.name
        case_ids = [
            _case_id(raw_case, owner=f"checked-in TCK suite {suite!r}")
            for raw_case in _load_json_array(
                cases_path,
                owner=f"checked-in TCK suite {suite!r}",
            )
        ]
        auxiliary_paths = tuple(
            path
            for path in sorted(cases_path.parent.glob("*.json"))
            if path.name != "cases.json"
        )
        if suite == "schema":
            for auxiliary_path in auxiliary_paths:
                case_ids.extend(
                    _case_id(
                        raw_case,
                        owner=f"checked-in TCK auxiliary fixture {auxiliary_path.name!r}",
                    )
                    for raw_case in _load_json_array(
                        auxiliary_path,
                        owner=f"checked-in TCK auxiliary fixture {auxiliary_path.name!r}",
                    )
                )
        if len(case_ids) != len(set(case_ids)):
            raise RuntimeError(f"checked-in TCK suite {suite!r} has duplicate case ids")
        fixture_digest = canonical_hash(
            {
                fixture.name: "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
                for fixture in sorted(cases_path.parent.glob("*.json"))
            }
        )
        contract: dict[str, object] = {
            "suite_id": suite,
            "path": cases_path.relative_to(tck_root).as_posix(),
            "case_count": len(case_ids),
            "case_ids": case_ids,
            "fixture_digest": fixture_digest,
        }
        if auxiliary_paths:
            contract["auxiliary_paths"] = [
                path.relative_to(tck_root).as_posix() for path in auxiliary_paths
            ]
        suite_manifest_digest = canonical_hash(contract)
        case_ids_digest = canonical_hash({"case_ids": case_ids})
        contracts.append(contract)
        suite_implementation = (
            "graphblocks-runtime"
            if suite in {"compiler", "runtime"}
            else implementation
        )
        suite_implementation_version = (
            native_compiler_version
            if suite in {"compiler", "runtime"}
            else implementation_version
        )
        suites[suite] = {
            "case_ids": tuple(case_ids),
            "case_ids_digest": case_ids_digest,
            "fixture_digest": fixture_digest,
            "implementation": suite_implementation,
            "implementation_version": suite_implementation_version,
            "suite_manifest_digest": suite_manifest_digest,
        }
        if suite in {"compiler", "runtime"}:
            suites[suite]["implementation_artifact_distribution"] = (
                "graphblocks-runtime"
            )
    if not suites:
        raise RuntimeError("bundled stable TCK contains no suites")
    profile_catalog_path = root / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"
    try:
        profile_documents = load_documents(profile_catalog_path)
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError("checked-in conformance profile catalog is invalid") from error
    if len(profile_documents) != 1:
        raise RuntimeError("checked-in conformance profile catalog must contain one document")
    profile_catalog = profile_documents[0]
    if not isinstance(profile_catalog, Mapping) or profile_catalog.get("kind") != (
        "ConformanceProfileSet"
    ):
        raise RuntimeError("checked-in conformance profile catalog is invalid")
    profile_spec = profile_catalog.get("spec")
    raw_profiles = profile_spec.get("profiles") if isinstance(profile_spec, Mapping) else None
    if not isinstance(raw_profiles, list):
        raise RuntimeError("checked-in conformance profile catalog is invalid")
    profiles: dict[str, Mapping[str, object]] = {}
    for raw_profile in raw_profiles:
        profile_id = raw_profile.get("id") if isinstance(raw_profile, Mapping) else None
        if not isinstance(profile_id, str) or not profile_id or profile_id in profiles:
            raise RuntimeError("checked-in conformance profile catalog has invalid profile ids")
        profiles[profile_id] = raw_profile

    included_profiles: set[str] = set()

    def include_profile(profile_id: str, active: tuple[str, ...] = ()) -> None:
        if profile_id in active:
            raise RuntimeError("checked-in conformance profiles contain an inheritance cycle")
        if profile_id in included_profiles:
            return
        profile = profiles.get(profile_id)
        if profile is None:
            raise RuntimeError(f"stable conformance profile is missing: {profile_id}")
        extends = profile.get("extends", [])
        if not isinstance(extends, list) or not all(
            isinstance(parent, str) and parent for parent in extends
        ):
            raise RuntimeError("checked-in conformance profile inheritance is invalid")
        for parent in extends:
            include_profile(parent, (*active, profile_id))
        included_profiles.add(profile_id)

    for profile_id in STABLE_CONFORMANCE_PROFILES:
        include_profile(profile_id)
    required_suites: set[str] = set()
    for profile_id in included_profiles:
        suites_value = profiles[profile_id].get("tck", [])
        if not isinstance(suites_value, list) or not all(
            isinstance(suite, str) and suite for suite in suites_value
        ):
            raise RuntimeError("checked-in conformance profile TCK requirements are invalid")
        required_suites.update(suites_value)
    if set(suites) != required_suites:
        raise RuntimeError(
            "bundled stable TCK suite set does not exactly cover C0/C1 profiles"
        )
    authority_matrix_path = root / "docs" / "project" / "stable-release-matrix.yaml"
    try:
        authority_documents = load_documents(authority_matrix_path)
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError("checked-in conformance authority matrix is invalid") from error
    if len(authority_documents) != 1 or not isinstance(
        authority_documents[0], Mapping
    ):
        raise RuntimeError(
            "checked-in conformance authority matrix must contain one document"
        )
    try:
        authority_matrix = ConformanceAuthorityMatrix.from_document(
            authority_documents[0]
        )
        observed_execution_claims = {
            suite: {
                "executor_id": (
                    "rust-compiler-exact-differential"
                    if suite == "compiler"
                    else (
                        "rust-runtime-exact-differential"
                        if suite == "runtime"
                        else "python-reference"
                    )
                ),
                "implementation": expectation["implementation"],
                "language": (
                    "rust" if suite in {"compiler", "runtime"} else "python"
                ),
                "comparison": (
                    "exact-native-reference"
                    if suite in {"compiler", "runtime"}
                    else "reference-only"
                ),
                "reference_implementation": "graphblocks-python",
            }
            for suite, expectation in suites.items()
        }
        authority_claim = authority_matrix.validate_tck_claims(
            claimed_profiles=STABLE_CONFORMANCE_PROFILES,
            declared_suites_by_profile={
                profile_id: tuple(profiles[profile_id].get("tck", ()))
                for profile_id in STABLE_CONFORMANCE_PROFILES
            },
            observed_execution_claims=observed_execution_claims,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "checked-in conformance authority TCK claims are invalid"
        ) from error
    raw_suite_authority_claims = authority_claim.get("suite_claims")
    if not isinstance(raw_suite_authority_claims, Mapping) or set(
        raw_suite_authority_claims
    ) != set(suites):
        raise RuntimeError(
            "checked-in conformance authority claims do not cover stable TCK suites"
        )
    for suite, expectation in suites.items():
        suite_authority_claim = raw_suite_authority_claims[suite]
        if not isinstance(suite_authority_claim, Mapping):
            raise RuntimeError(
                f"checked-in TCK suite {suite!r} authority claim is invalid"
            )
        expectation["authority_claim"] = dict(suite_authority_claim)
        expectation["execution_claim"] = dict(observed_execution_claims[suite])
        if suite_authority_claim.get("comparison") == "exact-native-reference":
            expectation["reference_implementation_version"] = implementation_version
    for suite in sorted(required_suites):
        bundled_suite_root = tck_root / suite
        source_suite_root = source_tck_root / suite
        bundled_names = {path.name for path in bundled_suite_root.glob("*.json")}
        source_names = {path.name for path in source_suite_root.glob("*.json")}
        if not bundled_names or bundled_names != source_names:
            raise RuntimeError(
                f"bundled stable TCK suite {suite!r} has an unexpected fixture set"
            )
        for fixture_name in sorted(bundled_names - {"cases.json"}):
            if (bundled_suite_root / fixture_name).read_bytes() != (
                source_suite_root / fixture_name
            ).read_bytes():
                raise RuntimeError(
                    f"bundled stable TCK suite {suite!r} auxiliary fixture differs "
                    "from checked-in content"
                )
        bundled_cases = _load_json_array(
            bundled_suite_root / "cases.json",
            owner=f"bundled stable TCK suite {suite!r}",
        )
        source_cases = _load_json_array(
            source_suite_root / "cases.json",
            owner=f"checked-in TCK suite {suite!r}",
        )
        source_by_id = {
            _case_id(case, owner=f"checked-in TCK suite {suite!r}"): (index, case)
            for index, case in enumerate(source_cases)
        }
        if len(source_by_id) != len(source_cases):
            raise RuntimeError(f"checked-in TCK suite {suite!r} has duplicate case ids")
        prior_source_index = -1
        for bundled_case in bundled_cases:
            case_id = _case_id(
                bundled_case,
                owner=f"bundled stable TCK suite {suite!r}",
            )
            source_entry = source_by_id.get(case_id)
            if (
                source_entry is None
                or bundled_case != source_entry[1]
                or source_entry[0] <= prior_source_index
            ):
                raise RuntimeError(
                    f"bundled stable TCK suite {suite!r} is not an ordered exact "
                    "subset of checked-in cases"
                )
            prior_source_index = source_entry[0]
    schema_manifest_digest = SchemaManifest.from_directory(
        root / "schemas"
    ).content_digest()
    return {
        "manifest_digest": canonical_hash({"suites": contracts}),
        "claimed_profiles": STABLE_CONFORMANCE_PROFILES,
        "authority_claim": authority_claim,
        "profile_catalog_digest": canonical_hash(profile_catalog),
        "schema_manifest_digest": schema_manifest_digest,
        "suites": suites,
    }


def _require_release_evidence(
    payload: object,
    *,
    kind: str,
    expected_tck: Mapping[str, object] | None = None,
    expected_acceptance: Mapping[str, object] | None = None,
    expected_compiler_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"installed {kind} evidence did not pass")
    if payload.get("ok") is not True:
        if kind == "TCK":
            failure_labels: list[str] = []
            failures_omitted = False
            reports = payload.get("reports")
            if isinstance(reports, Mapping):
                for suite, report in reports.items():
                    if not isinstance(suite, str) or not isinstance(report, Mapping):
                        continue
                    results = report.get("results")
                    if not isinstance(results, list):
                        continue
                    for result in results:
                        if not isinstance(result, Mapping) or result.get("status") == "passed":
                            continue
                        if len(failure_labels) == 20:
                            failures_omitted = True
                            break
                        case_id = result.get("case_id")
                        safe_suite = (
                            re.sub(r"[^\x20-\x7e]", "?", suite)[:64]
                            or "<invalid-suite>"
                        )
                        safe_case_id = (
                            re.sub(r"[^\x20-\x7e]", "?", case_id)[:128]
                            if isinstance(case_id, str) and case_id
                            else "<invalid-case>"
                        )
                        failure_labels.append(f"{safe_suite}/{safe_case_id}")
                    if failures_omitted:
                        break
            if failure_labels:
                suffix = "; additional failures omitted" if failures_omitted else ""
                raise RuntimeError(
                    "installed TCK evidence did not pass; failing cases: "
                    + "; ".join(failure_labels)
                    + suffix
                )
        raise RuntimeError(f"installed {kind} evidence did not pass")
    if kind == "TCK":
        reports = payload.get("reports")
        if not isinstance(reports, dict) or not reports:
            raise RuntimeError("installed TCK evidence contains no suite reports")
        raw_expected_suites = (
            expected_tck.get("suites") if isinstance(expected_tck, Mapping) else None
        )
        if expected_tck is not None:
            required_artifact_distributions = {
                expectation["implementation_artifact_distribution"]
                for expectation in (
                    raw_expected_suites.values()
                    if isinstance(raw_expected_suites, Mapping)
                    else ()
                )
                if isinstance(expectation, Mapping)
                and isinstance(
                    expectation.get("implementation_artifact_distribution"),
                    str,
                )
            }
            if required_artifact_distributions:
                if expected_compiler_artifact is None:
                    raise RuntimeError(
                        "checked-in TCK expectations require an exact native artifact"
                    )
                if required_artifact_distributions != {
                    expected_compiler_artifact.get("distribution")
                }:
                    raise RuntimeError(
                        "expected native artifact names another distribution"
                    )
            if payload.get("profile") != "local":
                raise RuntimeError("installed TCK evidence does not use the stable local profile")
            manifest_digest = _require_canonical_sha256(
                payload.get("suite_manifest_digest"),
                owner="installed TCK suite manifest digest",
            )
            if manifest_digest != expected_tck.get("manifest_digest"):
                raise RuntimeError("installed TCK evidence names another suite manifest")
            if payload.get("claimed_profiles") != list(
                expected_tck.get("claimed_profiles", ())
            ):
                raise RuntimeError("installed TCK evidence does not claim exact stable profiles")
            expected_authority_claim = expected_tck.get("authority_claim")
            observed_authority_claim = payload.get("authority_claim")
            if expected_authority_claim is not None:
                if not isinstance(expected_authority_claim, Mapping) or (
                    observed_authority_claim != expected_authority_claim
                ):
                    raise RuntimeError(
                        "installed TCK evidence does not match the authority matrix claim"
                    )
                _require_canonical_sha256(
                    expected_authority_claim.get("matrix_digest"),
                    owner="installed TCK authority matrix digest",
                )
            for field, label in (
                ("schema_manifest_digest", "schema manifest"),
                ("profile_catalog_digest", "conformance profile catalog"),
            ):
                digest = _require_canonical_sha256(
                    payload.get(field),
                    owner=f"installed TCK {label} digest",
                )
                if digest != expected_tck.get(field):
                    raise RuntimeError(f"installed TCK evidence names another {label}")
            if not isinstance(raw_expected_suites, Mapping) or set(reports) != set(
                raw_expected_suites
            ):
                raise RuntimeError(
                    "installed TCK evidence does not cover the exact checked-in suite set"
                )
        for suite, report in reports.items():
            if not isinstance(suite, str) or not suite:
                raise RuntimeError("installed TCK evidence contains an invalid suite id")
            if not isinstance(report, dict) or report.get("ok") is not True:
                raise RuntimeError(f"installed TCK suite {suite!r} did not pass")
            results = report.get("results")
            evidence = report.get("evidence")
            if not isinstance(results, list) or not results:
                raise RuntimeError(f"installed TCK suite {suite!r} contains no executed cases")
            if not isinstance(evidence, dict):
                raise RuntimeError(f"installed TCK suite {suite!r} contains no identity evidence")
            for field in ("fixture_digest", "implementation", "implementation_version", "suite"):
                if not isinstance(evidence.get(field), str) or not evidence[field].strip():
                    raise RuntimeError(
                        f"installed TCK suite {suite!r} has invalid evidence field {field!r}"
                    )
            _require_canonical_sha256(
                evidence["fixture_digest"],
                owner=f"installed TCK suite {suite!r} fixture digest",
            )
            if evidence["suite"] != suite:
                raise RuntimeError(f"installed TCK suite {suite!r} evidence names another suite")
            case_ids: set[str] = set()
            for result in results:
                if not isinstance(result, dict) or result.get("status") != "passed":
                    raise RuntimeError(f"installed TCK suite {suite!r} contains a non-passing result")
                case_id = result.get("case_id")
                if not isinstance(case_id, str) or not case_id.strip() or case_id in case_ids:
                    raise RuntimeError(f"installed TCK suite {suite!r} contains an invalid case id")
                case_ids.add(case_id)
            if isinstance(raw_expected_suites, Mapping):
                expectation = raw_expected_suites.get(suite)
                if not isinstance(expectation, Mapping):
                    raise RuntimeError(
                        f"installed TCK evidence contains unexpected suite {suite!r}"
                    )
                observed_case_ids = tuple(result.get("case_id") for result in results)
                expected_evidence_fields = [
                    "case_ids_digest",
                    "suite_manifest_digest",
                    "fixture_digest",
                    "implementation",
                    "implementation_version",
                ]
                if "authority_claim" in expectation:
                    expected_evidence_fields.append("authority_claim")
                if "execution_claim" in expectation:
                    expected_evidence_fields.append("execution_claim")
                if "reference_implementation_version" in expectation:
                    expected_evidence_fields.append(
                        "reference_implementation_version"
                    )
                for field in expected_evidence_fields:
                    if evidence.get(field) != expectation.get(field):
                        raise RuntimeError(
                            f"installed TCK suite {suite!r} {field!r} does not match checked-in expectations"
                        )
                _require_canonical_sha256(
                    evidence.get("case_ids_digest"),
                    owner=f"installed TCK suite {suite!r} case id digest",
                )
                if evidence.get("case_ids_digest") != canonical_hash(
                    {"case_ids": list(observed_case_ids)}
                ):
                    raise RuntimeError(
                        f"installed TCK suite {suite!r} case id digest does not "
                        "match executed cases"
                    )
                _require_canonical_sha256(
                    evidence.get("suite_manifest_digest"),
                    owner=f"installed TCK suite {suite!r} manifest digest",
                )
                expected_case_ids = expectation.get("case_ids")
                if not isinstance(expected_case_ids, (list, tuple)) or (
                    observed_case_ids != tuple(expected_case_ids)
                ):
                    raise RuntimeError(
                        f"installed TCK suite {suite!r} cases do not match checked-in expectations"
                    )
                execution_claim = expectation.get("execution_claim")
                if (
                    suite == "runtime"
                    and isinstance(execution_claim, Mapping)
                    and execution_claim.get("comparison")
                    == "exact-native-reference"
                ):
                    for result in results:
                        observed = result.get("observed")
                        if (
                            not isinstance(observed, Mapping)
                            or observed.get("runtime") != "native"
                            or observed.get("native_reference_match") is not True
                        ):
                            raise RuntimeError(
                                "installed runtime TCK evidence is not exact native/reference execution"
                            )
        if expected_compiler_artifact is not None:
            expected_artifact = dict(expected_compiler_artifact)
            for suite in ("compiler", "runtime"):
                report = reports.get(suite)
                evidence = (
                    report.get("evidence") if isinstance(report, Mapping) else None
                )
                if not isinstance(evidence, Mapping):
                    raise RuntimeError(
                        f"installed {suite} TCK evidence has no artifact identity"
                    )
                observed_artifact = evidence.get("implementation_artifact")
                if observed_artifact != expected_artifact:
                    raise RuntimeError(
                        f"installed {suite} TCK evidence does not bind the exact "
                        "graphblocks-runtime wheel"
                    )
                if (
                    evidence.get("implementation")
                    != expected_artifact.get("distribution")
                    or evidence.get("implementation_version")
                    != expected_artifact.get("version")
                ):
                    raise RuntimeError(
                        f"installed {suite} TCK implementation identity does not "
                        "match its wheel artifact"
                    )
    elif kind == "acceptance":
        manifest_digest = _require_canonical_sha256(
            payload.get("manifest_digest"),
            owner="installed acceptance manifest digest",
        )
        if expected_acceptance is None:
            raise RuntimeError("installed acceptance evidence has no checked-in expectations")
        if manifest_digest != expected_acceptance.get("manifest_digest"):
            raise RuntimeError("installed acceptance evidence names another manifest")
        raw_expected_applications = expected_acceptance.get("applications")
        if not isinstance(raw_expected_applications, Mapping):
            raise RuntimeError("installed acceptance expectations are invalid")
        applications = payload.get("applications")
        if not isinstance(applications, list) or not applications:
            raise RuntimeError("installed acceptance evidence contains no applications")
        application_ids: set[str] = set()
        for application in applications:
            if not isinstance(application, dict) or application.get("ok") is not True:
                raise RuntimeError("installed acceptance application did not pass")
            application_id = application.get("application_id")
            if (
                not isinstance(application_id, str)
                or not application_id.strip()
                or application_id in application_ids
            ):
                raise RuntimeError("installed acceptance evidence contains an invalid application id")
            application_ids.add(application_id)
            expectation = raw_expected_applications.get(application_id)
            if not isinstance(expectation, Mapping):
                raise RuntimeError(
                    f"installed acceptance evidence contains unexpected application {application_id!r}"
                )
            application_digest = _require_canonical_sha256(
                application.get("application_digest"),
                owner=f"installed acceptance application {application_id!r} digest",
            )
            scenario_digest = _require_canonical_sha256(
                application.get("scenario_digest"),
                owner=f"installed acceptance application {application_id!r} scenario digest",
            )
            if (
                application_digest != expectation.get("application_digest")
                or scenario_digest != expectation.get("scenario_digest")
                or application.get("scenario_path") != expectation.get("scenario_path")
            ):
                raise RuntimeError(
                    f"installed acceptance application {application_id!r} does not match checked-in content"
                )
            results = application.get("results")
            if not isinstance(results, list) or not results:
                raise RuntimeError("installed acceptance application contains no executed gates")
            gates: set[str] = set()
            for result in results:
                if not isinstance(result, dict) or result.get("status") != "passed":
                    raise RuntimeError("installed acceptance application contains a non-passing gate")
                if result.get("application_id") != application_id:
                    raise RuntimeError("installed acceptance gate names another application")
                gate = result.get("gate")
                if not isinstance(gate, str) or not gate.strip() or gate in gates:
                    raise RuntimeError("installed acceptance application contains an invalid gate id")
                gates.add(gate)
                output_digest = result.get("output_digest")
                if output_digest is not None:
                    _require_canonical_sha256(
                        output_digest,
                        owner=f"installed acceptance gate {gate!r} output digest",
                    )
            expected_gates = expectation.get("gates")
            if not isinstance(expected_gates, (list, tuple)) or tuple(
                result.get("gate") for result in results
            ) != tuple(expected_gates):
                raise RuntimeError(
                    f"installed acceptance application {application_id!r} gates do not match checked-in expectations"
                )
        if application_ids != set(raw_expected_applications):
            raise RuntimeError("installed acceptance evidence does not cover every application")
    else:  # pragma: no cover - internal misuse guard.
        raise ValueError(f"unknown release evidence kind {kind!r}")
    content_digest = _require_canonical_sha256(
        payload.get("contentDigest"),
        owner=f"installed {kind} evidence content digest",
    )
    unsigned_payload = dict(payload)
    unsigned_payload.pop("contentDigest")
    if content_digest != canonical_hash(unsigned_payload):
        raise RuntimeError(f"installed {kind} evidence content digest does not match its content")
    return payload


def _run_json_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    kind: str,
    expected_tck: Mapping[str, object] | None = None,
    expected_acceptance: Mapping[str, object] | None = None,
    expected_compiler_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    completed = subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout, parse_float=Decimal)
    except json.JSONDecodeError as error:
        if completed.returncode != 0:
            raw_detail = (
                completed.stderr
                if isinstance(completed.stderr, str) and completed.stderr.strip()
                else completed.stdout
            )
            if len(raw_detail) > 2_000:
                raw_detail = (
                    raw_detail[:500]
                    + " ... output omitted ... "
                    + raw_detail[-1_400:]
                )
            detail = re.sub(
                r"[^\x20-\x7e]",
                "?",
                " ".join(raw_detail.split()),
            )[:2_000]
            detail_suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"installed {kind} command exited with status "
                f"{completed.returncode} without valid JSON{detail_suffix}"
            ) from error
        raise RuntimeError(f"installed {kind} evidence is not valid JSON") from error
    validated = _require_release_evidence(
        payload,
        kind=kind,
        expected_tck=expected_tck,
        expected_acceptance=expected_acceptance,
        expected_compiler_artifact=expected_compiler_artifact,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"installed {kind} command exited with status {completed.returncode} "
            "despite passing evidence"
        )
    return validated


def release_evidence_expectations(root: Path = ROOT) -> dict[str, object]:
    return {
        "TCK": _tck_expectations(root),
        "acceptance": _acceptance_expectations(
            root / "acceptance" / "applications.yaml",
            root=root,
        ),
    }


def validate_release_evidence_payloads(
    *,
    tck_payload: object,
    acceptance_payload: object,
    root: Path = ROOT,
    expectations: Mapping[str, object] | None = None,
    expected_compiler_artifact: Mapping[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    frozen_expectations = (
        release_evidence_expectations(root) if expectations is None else expectations
    )
    expected_tck = frozen_expectations.get("TCK")
    expected_acceptance = frozen_expectations.get("acceptance")
    if not isinstance(expected_tck, Mapping) or not isinstance(
        expected_acceptance, Mapping
    ):
        raise RuntimeError("release evidence expectations are invalid")
    return {
        "TCK": _require_release_evidence(
            tck_payload,
            kind="TCK",
            expected_tck=expected_tck,
            expected_compiler_artifact=expected_compiler_artifact,
        ),
        "acceptance": _require_release_evidence(
            acceptance_payload,
            kind="acceptance",
            expected_acceptance=expected_acceptance,
        ),
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_identity(path: Path) -> tuple[str, str, str]:
    if path.suffix == ".whl":
        try:
            distribution, version, _build, _tags = parse_wheel_filename(path.name)
        except ValueError as error:
            raise RuntimeError(f"invalid first-party wheel filename: {path.name}") from error
        artifact_type = "wheel"
    elif path.name.endswith(".tar.gz"):
        try:
            distribution, version = parse_sdist_filename(path.name)
        except ValueError as error:
            raise RuntimeError(f"invalid PEP 625 sdist filename: {path.name}") from error
        artifact_type = "sdist"
    else:
        raise RuntimeError(f"unsupported first-party artifact filename: {path.name}")
    return canonicalize_name(str(distribution)), str(version), artifact_type


def _artifact_record(path: Path) -> dict[str, object]:
    distribution, version, artifact_type = _artifact_identity(path)
    return {
        "filename": path.name,
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
        "distribution": distribution,
        "version": version,
        "artifactType": artifact_type,
    }


def _safe_extract_sdist(sdist: Path, destination: Path) -> Path:
    """Extract one PEP 625 ``.tar.gz`` sdist without trusting archive paths/types."""
    try:
        distribution, version = parse_sdist_filename(sdist.name)
    except ValueError as error:
        raise RuntimeError(f"invalid PEP 625 sdist filename: {sdist.name}") from error
    expected_root = sdist.name.removesuffix(".tar.gz")
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    seen_casefolded: set[str] = set()
    total_size = 0
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_SDIST_MEMBER_COUNT:
                raise RuntimeError("sdist contains an invalid number of archive members")
            for member in members:
                if "\\" in member.name:
                    raise RuntimeError("sdist contains a non-portable archive path")
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or not member_path.parts
                    or any(part in {"", ".", ".."} for part in member_path.parts)
                    or any(
                        _is_nonportable_windows_path_part(part)
                        for part in member_path.parts
                    )
                    or member_path.parts[0] != expected_root
                ):
                    raise RuntimeError("sdist archive path escapes its PEP 625 root")
                normalized = member_path.as_posix()
                folded = normalized.casefold()
                if normalized in seen or folded in seen_casefolded:
                    raise RuntimeError("sdist contains duplicate archive paths")
                seen.add(normalized)
                seen_casefolded.add(folded)
                if not (member.isdir() or member.isfile()):
                    raise RuntimeError("sdist contains a link or special archive member")
                if member.size < 0:
                    raise RuntimeError("sdist contains an invalid archive member size")
                total_size += member.size
                if total_size > MAX_SDIST_UNPACKED_SIZE:
                    raise RuntimeError("sdist exceeds the maximum unpacked size")

            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("sdist regular file could not be read")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                try:
                    target.chmod(member.mode & 0o777)
                except OSError:
                    pass
    except (OSError, tarfile.TarError) as error:
        raise RuntimeError(f"sdist could not be extracted safely: {sdist.name}") from error

    extracted_root = destination / expected_root
    manifest = extracted_root / "pyproject.toml"
    if not extracted_root.is_dir() or not manifest.is_file() or manifest.is_symlink():
        raise RuntimeError("sdist does not contain one buildable PEP 625 source root")
    try:
        project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
        observed = (
            canonicalize_name(str(project["name"])),
            str(project["version"]),
        )
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError("sdist contains an invalid project manifest") from error
    if observed != (canonicalize_name(str(distribution)), str(version)):
        raise RuntimeError("sdist project name/version does not match its PEP 625 filename")
    return extracted_root


def _release_artifact_component(record: Mapping[str, object]) -> dict[str, object]:
    filename = str(record["filename"])
    digest = str(record["sha256"])
    return {
        "type": "file",
        "name": filename,
        "bom-ref": f"urn:sha256:{digest}",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            {"name": "graphblocks:release-artifact", "value": "true"},
            {"name": "graphblocks:distribution", "value": str(record["distribution"])},
            {"name": "graphblocks:version", "value": str(record["version"])},
            {"name": "graphblocks:artifact-type", "value": str(record["artifactType"])},
        ],
    }


def _generate_cyclonedx_sbom(
    *,
    python_environment: Path,
    output_path: Path,
    expected_distributions: Mapping[str, str],
    expected_artifacts: Mapping[str, Mapping[str, object]],
) -> None:
    command = [sys.executable, "-m", "cyclonedx_py"]
    try:
        version = subprocess.run(
            [*command, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"cyclonedx-bom=={CYCLONEDX_BOM_VERSION} is required to generate the release SBOM"
        ) from error
    if version != CYCLONEDX_BOM_VERSION:
        raise RuntimeError(
            f"release SBOM requires cyclonedx-bom=={CYCLONEDX_BOM_VERSION}, found {version!r}"
        )
    if output_path.exists():
        raise RuntimeError("release SBOM output must not already exist")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            *command,
            "environment",
            str(python_environment),
            "--pyproject",
            str(ROOT / "pyproject.toml"),
            "--mc-type",
            "library",
            "--sv",
            "1.6",
            "--output-reproducible",
            "--of",
            "JSON",
            "--output-file",
            str(output_path),
            "--validate",
        ],
        check=True,
        cwd=ROOT,
    )
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("generated release SBOM is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("bomFormat") != "CycloneDX":
        raise RuntimeError("generated release SBOM is not CycloneDX JSON")
    components = payload.get("components")
    metadata = payload.get("metadata")
    candidates = list(components) if isinstance(components, list) else []
    if isinstance(metadata, dict) and isinstance(metadata.get("component"), dict):
        candidates.append(metadata["component"])
    observed = {
        (canonicalize_name(str(component.get("name"))), str(component.get("version")))
        for component in candidates
        if isinstance(component, dict)
        and isinstance(component.get("name"), str)
        and isinstance(component.get("version"), str)
    }
    missing = sorted(
        f"{distribution}=={version}"
        for distribution, version in expected_distributions.items()
        if (distribution, version) not in observed
    )
    if missing:
        raise RuntimeError(
            "generated release SBOM omits runtime distributions: " + ", ".join(missing)
        )
    metadata_component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(metadata_component, dict):
        raise RuntimeError("generated release SBOM has no metadata component")
    metadata_name = metadata_component.get("name")
    metadata_version = metadata_component.get("version")
    metadata_reference = metadata_component.get("bom-ref")
    if (
        not isinstance(metadata_name, str)
        or not isinstance(metadata_version, str)
        or not isinstance(metadata_reference, str)
        or not metadata_reference
        or expected_distributions.get(canonicalize_name(metadata_name))
        != metadata_version
    ):
        raise RuntimeError(
            "generated release SBOM metadata component is outside the runtime closure"
        )
    raw_components = payload.get("components")
    normalized_components: list[dict[str, object]] = []
    runtime_references = {metadata_reference}
    bootstrap_references: set[str] = set()
    for component in raw_components if isinstance(raw_components, list) else []:
        if not isinstance(component, dict):
            raise RuntimeError("generated release SBOM contains a malformed component")
        properties = component.get("properties")
        if isinstance(properties, list) and any(
            isinstance(prop, dict)
            and prop.get("name") == "graphblocks:release-artifact"
            for prop in properties
        ):
            continue
        name = component.get("name")
        version = component.get("version")
        reference = component.get("bom-ref")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(reference, str)
            or not reference
        ):
            raise RuntimeError("generated release SBOM contains a malformed component")
        distribution = canonicalize_name(name)
        if expected_distributions.get(distribution) == version:
            normalized_components.append(component)
            runtime_references.add(reference)
        elif distribution in {"pip", "setuptools"} and (
            distribution not in expected_distributions
        ):
            bootstrap_references.add(reference)
        else:
            raise RuntimeError(
                "generated release SBOM contains a distribution outside the exact "
                f"runtime closure: {distribution}=={version}"
            )
    raw_dependencies = payload.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise RuntimeError("generated release SBOM has no dependency graph")
    normalized_dependencies: list[dict[str, object]] = []
    observed_dependency_rows: set[str] = set()
    for relationship in raw_dependencies:
        if not isinstance(relationship, dict):
            raise RuntimeError("generated release SBOM has a malformed dependency row")
        reference = relationship.get("ref")
        depends_on = relationship.get("dependsOn", [])
        if (
            not isinstance(reference, str)
            or not reference
            or not isinstance(depends_on, list)
            or not all(isinstance(item, str) and item for item in depends_on)
            or len(depends_on) != len(set(depends_on))
        ):
            raise RuntimeError("generated release SBOM has a malformed dependency row")
        if reference in bootstrap_references:
            continue
        if reference not in runtime_references or reference in observed_dependency_rows:
            raise RuntimeError(
                "generated release SBOM dependency graph escapes the runtime closure"
            )
        retained_dependencies = sorted(set(depends_on) - bootstrap_references)
        if any(dependency not in runtime_references for dependency in retained_dependencies):
            raise RuntimeError(
                "generated release SBOM dependency graph escapes the runtime closure"
            )
        observed_dependency_rows.add(reference)
        normalized_dependencies.append(
            {"ref": reference, "dependsOn": retained_dependencies}
        )
    if observed_dependency_rows != runtime_references:
        raise RuntimeError(
            "generated release SBOM dependency graph omits runtime components"
        )
    normalized_components.extend(
        _release_artifact_component(expected_artifacts[filename])
        for filename in sorted(expected_artifacts)
    )
    payload["components"] = normalized_components
    payload["dependencies"] = sorted(
        normalized_dependencies,
        key=lambda relationship: str(relationship["ref"]),
    )
    _write_utf8_lf(
        output_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _pinned_build_tool_identities() -> dict[str, str]:
    observed: dict[str, str] = {}
    for distribution, expected_version in PINNED_BUILD_TOOLS.items():
        try:
            observed_version = distribution_version(distribution)
        except PackageNotFoundError as error:
            raise RuntimeError(
                f"release builds require {distribution}=={expected_version}"
            ) from error
        if observed_version != expected_version:
            raise RuntimeError(
                f"release builds require {distribution}=={expected_version}, "
                f"found {observed_version!r}"
            )
        observed[distribution] = observed_version
    return observed


def _resolved_build_environment(*, declared_platform: str | None) -> dict[str, object]:
    resolved: dict[str, str] = {}
    for distribution in installed_distributions():
        name = distribution.metadata.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        canonical_name = canonicalize_name(name)
        version = distribution.version
        previous = resolved.get(canonical_name)
        if previous is not None and previous != version:
            raise RuntimeError(
                f"build environment contains conflicting {canonical_name!r} versions"
            )
        resolved[canonical_name] = version
    runner_name = os.environ.get("ImageOS") or declared_platform or platform_module.system()
    runner_version = (
        os.environ.get("ImageVersion")
        or os.environ.get("RUNNER_IMAGE_VERSION")
        or platform_module.platform()
    )
    return {
        "python": {
            "implementation": platform_module.python_implementation(),
            "version": platform_module.python_version(),
        },
        "platform": platform_module.platform(),
        "runnerImage": {"name": runner_name, "version": runner_version},
        "resolvedDistributions": [
            {"name": name, "version": resolved[name]} for name in sorted(resolved)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify the catalog's first-party Python distributions offline."
    )
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument(
        "--sdist-dir",
        type=Path,
        help="directory retaining one PEP 625 source distribution per first-party package",
    )
    parser.add_argument(
        "--dependency-wheelhouse",
        type=Path,
        help="separate cache for third-party install-only wheels",
    )
    parser.add_argument(
        "--release-evidence-dir",
        type=Path,
        help="run installed-artifact TCK and acceptance gates and retain their JSON evidence",
    )
    parser.add_argument(
        "--sbom-output",
        type=Path,
        help="generate a reproducible CycloneDX SBOM from the isolated installed wheelhouse",
    )
    parser.add_argument(
        "--platform",
        help="stable release matrix operating-system id recorded with retained evidence",
    )
    parser.add_argument(
        "--python-version",
        help="stable release matrix Python major.minor recorded with retained evidence",
    )
    parser.add_argument(
        "--rustc",
        default="rustc",
        help="rustc executable whose observed identity is bound to platform evidence",
    )
    args = parser.parse_args(argv)

    wheelhouse = args.wheelhouse.resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if any(wheelhouse.iterdir()):
        raise ValueError(
            "wheelhouse must not contain existing wheel artifacts or other entries"
        )
    sdist_dir = (
        args.sdist_dir.resolve()
        if args.sdist_dir is not None
        else wheelhouse.parent / f"{wheelhouse.name}-sdists"
    )
    if sdist_dir == wheelhouse:
        raise ValueError("sdist directory must be separate from release wheelhouse")
    if sdist_dir.is_relative_to(wheelhouse) or wheelhouse.is_relative_to(sdist_dir):
        raise ValueError("release wheelhouse and sdist directory must not overlap")
    sdist_dir.mkdir(parents=True, exist_ok=True)
    if any(sdist_dir.iterdir()):
        raise ValueError("sdist directory must be empty")
    dependency_wheelhouse = (
        args.dependency_wheelhouse.resolve()
        if args.dependency_wheelhouse is not None
        else wheelhouse.parent / f"{wheelhouse.name}-dependencies"
    )
    if dependency_wheelhouse == wheelhouse:
        raise ValueError("dependency wheelhouse must be separate from release wheelhouse")
    release_roots = (wheelhouse, sdist_dir)
    if any(
        dependency_wheelhouse.is_relative_to(release_root)
        or release_root.is_relative_to(dependency_wheelhouse)
        for release_root in release_roots
    ):
        raise ValueError("release artifact and dependency directories must not overlap")
    dependency_wheelhouse.mkdir(parents=True, exist_ok=True)
    if any(dependency_wheelhouse.iterdir()):
        raise ValueError("dependency wheelhouse must be empty")
    if (args.platform is None) != (args.python_version is None):
        raise ValueError("platform and Python version must be provided together")
    if args.platform is not None and args.release_evidence_dir is None:
        raise ValueError("platform identity requires retained release evidence")
    if args.platform is not None and args.sbom_output is None:
        raise ValueError("platform identity requires a retained platform SBOM")
    if args.python_version is not None and platform_module.python_version_tuple()[:2] != tuple(
        args.python_version.split(".")
    ):
        raise RuntimeError("requested Python version does not match the running interpreter")
    if args.platform is not None and (
        args.platform,
        args.python_version,
    ) not in SUPPORTED_PLATFORM_MATRIX:
        raise ValueError("platform identity is not in the supported release matrix")
    rustc_identity = observe_rustc_identity(args.rustc)
    build_tools = {
        **_pinned_build_tool_identities(),
        "rustc": rustc_identity["version"],
    }
    build_identity = _resolved_build_environment(declared_platform=args.platform)
    build_environment = dict(os.environ)
    build_environment["PATH"] = (
        f"{Path(sys.executable).absolute().parent}{os.pathsep}"
        f"{build_environment.get('PATH', '')}"
    )
    build_environment["SOURCE_DATE_EPOCH"] = "315532800"
    build_environment["PYTHONHASHSEED"] = "0"
    build_environment["RUSTC"] = args.rustc
    catalog = load_package_catalog()
    matrix = build_wheel_matrix(ROOT, catalog=catalog)
    if not matrix.ok:
        raise RuntimeError(f"first-party wheel matrix is invalid: {matrix.diagnostics!r}")
    manifests = tuple(ROOT / target.manifest for target in matrix.targets)
    expected_distributions: dict[str, str] = {}
    with TemporaryDirectory(prefix="graphblocks-sdist-build-") as build_root_name, TemporaryDirectory(
        prefix="graphblocks-sdist-extract-"
    ) as extraction_root_name:
        build_root = Path(build_root_name)
        extraction_root = Path(extraction_root_name)
        for index, manifest in enumerate(manifests):
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
            distribution = canonicalize_name(str(project["name"]))
            version = str(project["version"])
            if distribution in expected_distributions:
                raise RuntimeError(f"duplicate first-party distribution: {distribution}")
            expected_distributions[distribution] = version

            isolated_output = build_root / str(index)
            isolated_output.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--sdist",
                    "--no-isolation",
                    "--outdir",
                    str(isolated_output),
                    str(manifest.parent),
                ],
                check=True,
                cwd=ROOT,
                env=build_environment,
            )
            candidates = tuple(isolated_output.iterdir())
            if len(candidates) != 1 or not candidates[0].name.endswith(".tar.gz"):
                raise RuntimeError(
                    f"expected one PEP 625 sdist for {distribution}, found {len(candidates)}"
                )
            source_sdist = candidates[0]
            observed_distribution, observed_version, artifact_type = _artifact_identity(
                source_sdist
            )
            if (
                artifact_type != "sdist"
                or observed_distribution != distribution
                or observed_version != version
            ):
                raise RuntimeError(
                    f"sdist filename does not match {distribution}=={version}"
                )
            retained_sdist = sdist_dir / source_sdist.name
            if retained_sdist.exists():
                raise RuntimeError(f"duplicate first-party sdist filename: {source_sdist.name}")
            shutil.copy2(source_sdist, retained_sdist)
            extracted_source = _safe_extract_sdist(
                retained_sdist,
                extraction_root / str(index),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    str(wheelhouse),
                    str(extracted_source),
                ],
                check=True,
                cwd=extracted_source,
                env=build_environment,
            )

    built_wheels = tuple(sorted(wheelhouse.glob("*.whl")))
    built_sdists = tuple(sorted(sdist_dir.glob("*.tar.gz")))
    if len(built_wheels) != len(manifests):
        raise RuntimeError(
            f"expected {len(manifests)} first-party wheel artifacts, found {len(built_wheels)}"
        )
    if len(built_sdists) != len(manifests):
        raise RuntimeError(
            f"expected {len(manifests)} first-party sdist artifacts, found {len(built_sdists)}"
        )
    base_graphblocks_wheels = tuple(
        path
        for path in built_wheels
        if _artifact_identity(path)[:2]
        == ("graphblocks", expected_distributions["graphblocks"])
    )
    if len(base_graphblocks_wheels) != 1:
        raise RuntimeError(
            "first-party wheelhouse must contain exactly one base graphblocks artifact"
        )
    base_graphblocks_wheel = base_graphblocks_wheels[0]
    native_compiler_wheels = tuple(
        path
        for path in built_wheels
        if _artifact_identity(path)[:2]
        == (
            "graphblocks-runtime",
            expected_distributions["graphblocks-runtime"],
        )
    )
    if len(native_compiler_wheels) != 1:
        raise RuntimeError(
            "first-party wheelhouse must contain exactly one "
            "graphblocks-runtime compiler artifact"
        )
    native_compiler_wheel = native_compiler_wheels[0]
    native_compiler_artifact = _artifact_record(native_compiler_wheel)
    observed_sdist_versions = {
        distribution: version
        for distribution, version, artifact_type in (
            _artifact_identity(path) for path in built_sdists
        )
        if artifact_type == "sdist"
    }
    if observed_sdist_versions != expected_distributions:
        raise RuntimeError("first-party sdists do not match the exact package catalog")

    with TemporaryDirectory(prefix="graphblocks-wheel-download-") as download_root:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                download_root,
                *(str(wheel) for wheel in built_wheels),
            ],
            check=True,
            cwd=ROOT,
        )
        for downloaded_wheel in sorted(Path(download_root).glob("*.whl")):
            try:
                distribution, _version, _build, _tags = parse_wheel_filename(
                    downloaded_wheel.name
                )
            except ValueError as error:
                raise RuntimeError(
                    f"dependency resolver produced an invalid wheel: {downloaded_wheel.name}"
                ) from error
            if canonicalize_name(str(distribution)) in expected_distributions:
                continue
            destination = dependency_wheelhouse / downloaded_wheel.name
            if destination.exists():
                if destination.read_bytes() != downloaded_wheel.read_bytes():
                    raise RuntimeError(
                        f"dependency resolver produced conflicting wheels: {downloaded_wheel.name}"
                    )
                continue
            shutil.copy2(downloaded_wheel, destination)

    expected_schema_manifest = SchemaManifest.from_directory(ROOT / "schemas").manifest_payload()
    with TemporaryDirectory(prefix="graphblocks-wheelhouse-") as install_root:
        venv.EnvBuilder(with_pip=True).create(install_root)
        isolated_python = Path(install_root) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_environment = dict(os.environ)
        install_environment.pop("PYTHONHOME", None)
        install_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(isolated_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--find-links",
                str(dependency_wheelhouse),
                *(str(wheel) for wheel in built_wheels),
            ],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        subprocess.run(
            [str(isolated_python), "-m", "pip", "check"],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        subprocess.run(
            [
                str(isolated_python),
                "-c",
                (
                    "import importlib; "
                    "from graphblocks.packages import load_package_catalog; "
                    "import graphblocks, graphblocks_runtime, graphblocks_testing; "
                    "importlib.import_module('graphblocks_runtime._native'); "
                    "catalog = load_package_catalog(); "
                    "[importlib.import_module(item['import']) for item in catalog['components'] if item.get('import')]"
                ),
            ],
            check=True,
            cwd=ROOT,
            env=install_environment,
        )
        native_authority_probe = Path(install_root) / "native-authority-probe.py"
        _write_utf8_lf(
            native_authority_probe,
            installed_native_authority_probe_source() + "\n",
        )
        installed_native_binding = subprocess.run(
            [
                str(isolated_python),
                str(native_authority_probe),
            ],
            check=True,
            cwd=install_root,
            env=install_environment,
            capture_output=True,
            text=True,
        )
        try:
            installed_native_binding_payload = json.loads(
                installed_native_binding.stdout
            )
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "installed native binding handshake is not valid JSON"
            ) from error
        _validate_installed_native_binding(
            installed_native_binding_payload,
            expected_distribution_version=str(
                native_compiler_artifact["version"]
            ),
        )
        if _artifact_record(native_compiler_wheel) != native_compiler_artifact:
            raise RuntimeError(
                "graphblocks-runtime compiler wheel changed during installation"
            )
        installed_schema_manifest = subprocess.run(
            [str(isolated_python), "-m", "graphblocks", "schemas", "manifest"],
            check=True,
            cwd=install_root,
            env=install_environment,
            capture_output=True,
            text=True,
        )
        try:
            installed_schema_payload = json.loads(installed_schema_manifest.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("installed schema manifest is not valid JSON") from error
        if installed_schema_payload != expected_schema_manifest:
            raise RuntimeError(
                "installed schema manifest does not match the checked-in source manifest"
            )
        if args.release_evidence_dir is not None:
            evidence_root = args.release_evidence_dir.resolve()
            evidence_root.mkdir(parents=True, exist_ok=True)
            if any(evidence_root.iterdir()):
                raise RuntimeError("release evidence directory must be empty")
            tck_command = Path(install_root) / (
                "Scripts/graphblocks-tck.exe" if os.name == "nt" else "bin/graphblocks-tck"
            )
            evidence_expectations = release_evidence_expectations(ROOT)
            tck_payload = _run_json_command(
                [
                    str(tck_command),
                    "run-all",
                    "--native-compiler-wheel",
                    str(native_compiler_wheel.resolve()),
                    "--json",
                ],
                cwd=install_root,
                env=install_environment,
                kind="TCK",
                expected_tck=evidence_expectations["TCK"],
                expected_compiler_artifact=native_compiler_artifact,
            )
            if _artifact_record(native_compiler_wheel) != native_compiler_artifact:
                raise RuntimeError(
                    "graphblocks-runtime compiler wheel changed during TCK execution"
                )
            acceptance_payload = _run_json_command(
                [
                    str(tck_command),
                    "run-acceptance",
                    str(ROOT / "acceptance" / "applications.yaml"),
                    "--root",
                    str(ROOT),
                    "--json",
                ],
                cwd=ROOT,
                env=install_environment,
                kind="acceptance",
                expected_acceptance=evidence_expectations["acceptance"],
            )
            _write_utf8_lf(
                evidence_root / "tck.json",
                canonical_dumps(tck_payload) + "\n",
            )
            _write_utf8_lf(
                evidence_root / "acceptance.json",
                canonical_dumps(acceptance_payload) + "\n",
            )
        expected_runtime_distributions = dict(expected_distributions)
        for dependency_wheel in sorted(dependency_wheelhouse.glob("*.whl")):
            try:
                distribution, version, _build, _tags = parse_wheel_filename(
                    dependency_wheel.name
                )
            except ValueError as error:
                raise RuntimeError(
                    f"dependency wheelhouse contains an invalid wheel: {dependency_wheel.name}"
                ) from error
            name = canonicalize_name(str(distribution))
            observed_version = str(version)
            previous = expected_runtime_distributions.get(name)
            if previous is not None and previous != observed_version:
                raise RuntimeError(
                    f"dependency wheelhouse contains conflicting versions for {name}"
                )
            expected_runtime_distributions[name] = observed_version
        if args.sbom_output is not None:
            if args.platform is not None and args.sbom_output.resolve() != (
                evidence_root / "sbom.cdx.json"
            ):
                raise ValueError("platform SBOM must be retained in the platform evidence directory")
            artifact_records = {
                path.name: _artifact_record(path) for path in (*built_wheels, *built_sdists)
            }
            _generate_cyclonedx_sbom(
                python_environment=isolated_python,
                output_path=args.sbom_output.resolve(),
                expected_distributions=expected_runtime_distributions,
                expected_artifacts=artifact_records,
            )
        installed = subprocess.run(
            [str(isolated_python), "-m", "pip", "list", "--format=json"],
            check=True,
            cwd=ROOT,
            capture_output=True,
            env=install_environment,
            text=True,
        )
        installed_distributions = {
            canonicalize_name(str(distribution["name"])): str(distribution["version"])
            for distribution in json.loads(installed.stdout)
        }
        installed_runtime_distributions = {
            distribution: installed_distributions.get(distribution)
            for distribution in expected_runtime_distributions
        }
        if installed_runtime_distributions != expected_runtime_distributions:
            raise RuntimeError(
                "offline wheelhouse installation does not match the resolved runtime closure: "
                f"expected {expected_runtime_distributions!r}, "
                f"observed {installed_runtime_distributions!r}"
            )
        if args.platform is not None:
            artifact_records = [
                _artifact_record(path)
                for path in sorted((*built_wheels, *built_sdists), key=lambda item: item.name)
            ]
            platform_evidence = {
                "formatVersion": 1,
                "platform": {
                    "os": args.platform,
                    "python": args.python_version,
                },
                "artifacts": artifact_records,
                "buildTools": build_tools,
                "buildEnvironment": build_identity,
                "installedDistributions": [
                    {"name": name, "version": expected_runtime_distributions[name]}
                    for name in sorted(expected_runtime_distributions)
                ],
                "observedToolIdentities": {"rustc": rustc_identity},
                "sourceDateEpoch": build_environment["SOURCE_DATE_EPOCH"],
                "evidence": {
                    "tck": tck_payload["contentDigest"],
                    "acceptance": acceptance_payload["contentDigest"],
                },
                "contracts": {
                    "claimedProfiles": list(
                        evidence_expectations["TCK"]["claimed_profiles"]
                    ),
                    "conformanceProfileCatalogDigest": evidence_expectations["TCK"][
                        "profile_catalog_digest"
                    ],
                    "authorityMatrixDigest": evidence_expectations["TCK"][
                        "authority_claim"
                    ]["matrix_digest"],
                    "schemaManifestDigest": evidence_expectations["TCK"][
                        "schema_manifest_digest"
                    ],
                },
                "nativeCanonicalSchemaAuthority": {
                    "runtimeArtifact": dict(native_compiler_artifact),
                    "probe": installed_native_binding_payload,
                },
            }
            platform_evidence["contentDigest"] = canonical_hash(platform_evidence)
            _write_utf8_lf(
                evidence_root / "platform.json",
                json.dumps(platform_evidence, indent=2, sort_keys=True) + "\n",
            )

    package_boundary = yaml.safe_load(
        (ROOT / "compatibility" / "python-package-boundaries.yaml").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(package_boundary, Mapping):
        raise ValueError("Python package boundary must contain a mapping")
    boundary_dependencies = package_boundary.get("dependencies")
    if not isinstance(boundary_dependencies, Mapping):
        raise ValueError("Python package boundary must define dependencies")
    expected_base_requirements = boundary_dependencies.get("base")
    if not isinstance(expected_base_requirements, list) or not all(
        isinstance(requirement, str) and requirement
        for requirement in expected_base_requirements
    ):
        raise ValueError("Python package boundary base dependencies are invalid")
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    if root_project.get("dependencies") != expected_base_requirements:
        raise RuntimeError(
            "graphblocks project dependencies do not match the package boundary"
        )
    expected_metadata_requirements = list(expected_base_requirements)
    optional_dependencies = root_project.get("optional-dependencies")
    if not isinstance(optional_dependencies, Mapping):
        raise ValueError("graphblocks project optional dependencies are invalid")
    for extra, requirements in optional_dependencies.items():
        if not isinstance(extra, str) or not extra or not isinstance(requirements, list):
            raise ValueError("graphblocks project optional dependency group is invalid")
        for requirement in requirements:
            if not isinstance(requirement, str) or not requirement:
                raise ValueError(
                    "graphblocks project optional dependency requirement is invalid"
                )
            expected_metadata_requirements.append(
                f"{requirement}; extra == '{extra}'"
            )
    root_facade = package_boundary.get("rootFacade")
    if not isinstance(root_facade, Mapping):
        raise ValueError("Python package boundary must define the root facade")
    stable_surface_path = root_facade.get("stableSurface")
    if not isinstance(stable_surface_path, str) or not stable_surface_path:
        raise ValueError("Python package boundary stable surface path is invalid")
    stable_surface = yaml.safe_load(
        (ROOT / stable_surface_path).read_text(encoding="utf-8")
    )
    if not isinstance(stable_surface, Mapping) or not isinstance(
        stable_surface.get("symbols"), list
    ):
        raise ValueError("stable Python surface is invalid")
    expected_root_exports: list[str] = []
    for entry in stable_surface["symbols"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise ValueError("stable Python surface contains an invalid symbol")
        symbol_parts = entry["path"].split(".", 2)
        if len(symbol_parts) < 2 or symbol_parts[0] != "graphblocks":
            raise ValueError("stable Python surface contains an invalid symbol path")
        root_export = symbol_parts[1]
        if root_export not in expected_root_exports:
            expected_root_exports.append(root_export)
    cold_import_budgets = package_boundary.get("coldImportBudgets")
    if not isinstance(cold_import_budgets, Mapping):
        raise ValueError("Python package boundary must define cold import budgets")
    root_import_budget = cold_import_budgets.get("graphblocks")
    canonical_import_budget = cold_import_budgets.get("graphblocks.canonical")
    if not isinstance(root_import_budget, Mapping) or not isinstance(
        canonical_import_budget, Mapping
    ):
        raise ValueError("Python package boundary import budgets are invalid")
    expected_root_modules = root_import_budget.get("allowedGraphblocksModules")
    expected_canonical_modules = canonical_import_budget.get(
        "allowedGraphblocksModules"
    )
    max_root_attributes = root_import_budget.get("maxRootAttributes")
    if (
        not isinstance(expected_root_modules, list)
        or not all(isinstance(module, str) and module for module in expected_root_modules)
        or not isinstance(expected_canonical_modules, list)
        or not all(
            isinstance(module, str) and module for module in expected_canonical_modules
        )
        or not isinstance(max_root_attributes, int)
        or isinstance(max_root_attributes, bool)
    ):
        raise ValueError("Python package boundary installed import contract is invalid")

    with TemporaryDirectory(prefix="graphblocks-base-install-") as base_install_root:
        venv.EnvBuilder(with_pip=True).create(base_install_root)
        base_python = Path(base_install_root) / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        base_environment = dict(os.environ)
        base_environment.pop("PYTHONHOME", None)
        base_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [
                str(base_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(dependency_wheelhouse),
                str(base_graphblocks_wheel),
            ],
            check=True,
            cwd=base_install_root,
            env=base_environment,
        )
        subprocess.run(
            [str(base_python), "-m", "pip", "check"],
            check=True,
            cwd=base_install_root,
            env=base_environment,
        )
        base_probe = subprocess.run(
            [str(base_python), "-c", BASE_GRAPHBLOCKS_INSTALL_PROBE],
            check=True,
            cwd=base_install_root,
            env=base_environment,
            capture_output=True,
            text=True,
        )
        try:
            base_payload = json.loads(base_probe.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "base graphblocks install probe did not return valid JSON"
            ) from error
        validate_base_graphblocks_install(
            base_payload,
            expected_version=expected_distributions["graphblocks"],
            expected_requirements=expected_metadata_requirements,
            expected_root_exports=expected_root_exports,
            expected_root_modules=expected_root_modules,
            expected_canonical_modules=expected_canonical_modules,
            max_root_attributes=max_root_attributes,
        )

    print(
        f"verified {len(manifests)} first-party wheels built from "
        f"{len(built_sdists)} retained sdists"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
