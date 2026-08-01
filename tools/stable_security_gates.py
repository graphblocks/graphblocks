from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

import yaml

from graphblocks.canonical import canonical_dumps, canonical_hash


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "docs/project/stable-security-gates.yaml"
MANIFEST_MAX_BYTES = 256 * 1024
RESULT_MAX_BYTES = 4 * 1024 * 1024
JUNIT_MAX_BYTES = 64 * 1024 * 1024
SOURCE_MAX_BYTES = 32 * 1024 * 1024
GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
PYTEST_SELECTOR = re.compile(
    r"(?P<path>tests/[A-Za-z0-9_./-]+\.py)::"
    r"(?P<name>test_[A-Za-z0-9_]+(?:\[[^\]\r\n]+\])?)"
)
ARTIFACT_NAME_PREFIX = "graphblocks-stable-security-gates"
ARTIFACT_NAME = re.compile(rf"{re.escape(ARTIFACT_NAME_PREFIX)}-[1-9][0-9]*")
RESULT_FILE = "stable-security-gates.json"
JUNIT_FILE = "stable-security-gates.xml"
RUNNER_OS = "ubuntu-latest"
RUNNER_PYTHON = "3.11"


class StableSecurityGateError(RuntimeError):
    """Stable security-gate evidence is incomplete, malformed, or untrusted."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise StableSecurityGateError(
                "stable security-gate manifest keys must be scalar"
            ) from error
        if duplicate:
            raise StableSecurityGateError(
                f"stable security-gate manifest repeats YAML key {key!r}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _require_exact_mapping(
    value: object,
    keys: set[str],
    *,
    owner: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise StableSecurityGateError(f"{owner} must have the exact closed shape")
    if any(type(key) is not str for key in value):
        raise StableSecurityGateError(f"{owner} keys must be strings")
    return dict(value)


def _require_non_empty_string(value: object, *, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise StableSecurityGateError(f"{owner} must be a non-empty trimmed string")
    return value


def _require_safe_repository_path(value: object, *, owner: str) -> str:
    path = _require_non_empty_string(value, owner=owner)
    pure_path = PurePosixPath(path)
    if (
        pure_path.is_absolute()
        or pure_path.as_posix() != path
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or "\\" in path
    ):
        raise StableSecurityGateError(f"{owner} must be a normalized repository path")
    return path


def _selector_source_path(value: object, *, owner: str) -> str:
    selector = _require_non_empty_string(value, owner=owner)
    match = PYTEST_SELECTOR.fullmatch(selector)
    if match is None:
        raise StableSecurityGateError(
            f"{owner} must be one exact tests/*.py::test_* pytest selector"
        )
    return _require_safe_repository_path(
        match.group("path"),
        owner=f"{owner} source path",
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (canonical_dumps(value) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_regular_file(path: Path, *, owner: str, max_bytes: int) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StableSecurityGateError(f"{owner} must be a regular non-symlink file")
        if metadata.st_size > max_bytes:
            raise StableSecurityGateError(f"{owner} exceeds {max_bytes} bytes")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
            opened_metadata = os.fstat(stream.fileno())
    except OSError as error:
        raise StableSecurityGateError(f"{owner} could not be read") from error
    if len(data) > max_bytes:
        raise StableSecurityGateError(f"{owner} exceeds {max_bytes} bytes")
    if not os.path.samestat(metadata, opened_metadata):
        raise StableSecurityGateError(f"{owner} changed while it was read")
    return data


def load_manifest_bytes(data: bytes) -> dict[str, object]:
    if type(data) is not bytes or len(data) > MANIFEST_MAX_BYTES:
        raise StableSecurityGateError("stable security-gate manifest is oversized")
    try:
        decoded = data.decode("utf-8")
        raw_manifest = yaml.load(decoded, Loader=_UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError) as error:
        raise StableSecurityGateError(
            "stable security-gate manifest is not valid UTF-8 YAML"
        ) from error
    manifest = _require_exact_mapping(
        raw_manifest,
        {
            "formatVersion",
            "resultFormatVersion",
            "artifact",
            "runner",
            "objectAuthorization",
            "adversarialResources",
        },
        owner="stable security-gate manifest",
    )
    if type(manifest.get("formatVersion")) is not int or manifest["formatVersion"] != 1:
        raise StableSecurityGateError("stable security-gate formatVersion must be 1")
    if (
        type(manifest.get("resultFormatVersion")) is not int
        or manifest["resultFormatVersion"] != 1
    ):
        raise StableSecurityGateError(
            "stable security-gate resultFormatVersion must be 1"
        )

    artifact = _require_exact_mapping(
        manifest.get("artifact"),
        {"namePrefix", "resultFile", "junitFile"},
        owner="stable security-gate artifact",
    )
    if artifact != {
        "namePrefix": ARTIFACT_NAME_PREFIX,
        "resultFile": RESULT_FILE,
        "junitFile": JUNIT_FILE,
    }:
        raise StableSecurityGateError(
            "stable security-gate artifact identity is not canonical"
        )
    runner = _require_exact_mapping(
        manifest.get("runner"),
        {"os", "python"},
        owner="stable security-gate runner",
    )
    if runner != {"os": RUNNER_OS, "python": RUNNER_PYTHON}:
        raise StableSecurityGateError(
            "stable security-gate runner must be ubuntu-latest Python 3.11"
        )

    object_authorization = _require_exact_mapping(
        manifest.get("objectAuthorization"),
        {"routeManifestSource", "categories"},
        owner="stable object-authorization manifest",
    )
    route_manifest_source = _require_safe_repository_path(
        object_authorization.get("routeManifestSource"),
        owner="stable object-authorization route manifest source",
    )
    if not route_manifest_source.endswith(".py"):
        raise StableSecurityGateError(
            "stable object-authorization route manifest source must be Python"
        )

    def normalize_categories(value: object, *, owner: str) -> list[dict[str, object]]:
        if not isinstance(value, list) or not value:
            raise StableSecurityGateError(f"{owner} must be a non-empty array")
        normalized: list[dict[str, object]] = []
        category_ids: set[str] = set()
        for index, raw_category in enumerate(value):
            category = _require_exact_mapping(
                raw_category,
                {"id", "pytestSelectors"},
                owner=f"{owner} category {index}",
            )
            category_id = _require_non_empty_string(
                category.get("id"),
                owner=f"{owner} category {index} id",
            )
            selectors = category.get("pytestSelectors")
            if not isinstance(selectors, list) or not selectors:
                raise StableSecurityGateError(
                    f"{owner} category {category_id} must select tests"
                )
            normalized_selectors: list[str] = []
            for selector_index, selector in enumerate(selectors):
                normalized_selector = _require_non_empty_string(
                    selector,
                    owner=(f"{owner} category {category_id} selector {selector_index}"),
                )
                _selector_source_path(
                    normalized_selector,
                    owner=(f"{owner} category {category_id} selector {selector_index}"),
                )
                normalized_selectors.append(normalized_selector)
            if category_id in category_ids or len(normalized_selectors) != len(
                set(normalized_selectors)
            ):
                raise StableSecurityGateError(
                    f"{owner} category ids and selectors must be unique"
                )
            category_ids.add(category_id)
            normalized.append(
                {"id": category_id, "pytestSelectors": normalized_selectors}
            )
        return normalized

    object_categories = normalize_categories(
        object_authorization.get("categories"),
        owner="stable object-authorization manifest",
    )
    adversarial_resources = _require_exact_mapping(
        manifest.get("adversarialResources"),
        {"categories"},
        owner="stable adversarial-resource manifest",
    )
    adversarial_categories = normalize_categories(
        adversarial_resources.get("categories"),
        owner="stable adversarial-resource manifest",
    )
    if len(object_categories) != 5 or len(adversarial_categories) != 5:
        raise StableSecurityGateError(
            "stable security gates require exactly five authorization and resource categories"
        )
    return {
        "formatVersion": 1,
        "resultFormatVersion": 1,
        "artifact": dict(artifact),
        "runner": dict(runner),
        "objectAuthorization": {
            "routeManifestSource": route_manifest_source,
            "categories": object_categories,
        },
        "adversarialResources": {"categories": adversarial_categories},
    }


def load_manifest(path: Path = ROOT / MANIFEST_PATH) -> tuple[dict[str, object], bytes]:
    data = _read_regular_file(
        path,
        owner="stable security-gate manifest",
        max_bytes=MANIFEST_MAX_BYTES,
    )
    return load_manifest_bytes(data), data


def manifest_selectors(manifest: Mapping[str, object]) -> tuple[str, ...]:
    selectors: list[str] = []
    for section_name in ("objectAuthorization", "adversarialResources"):
        section = manifest[section_name]
        assert isinstance(section, Mapping)
        categories = section["categories"]
        assert isinstance(categories, Sequence)
        for category in categories:
            assert isinstance(category, Mapping)
            for selector in category["pytestSelectors"]:
                assert isinstance(selector, str)
                if selector not in selectors:
                    selectors.append(selector)
    return tuple(selectors)


def manifest_source_paths(manifest: Mapping[str, object]) -> tuple[str, ...]:
    object_authorization = manifest["objectAuthorization"]
    assert isinstance(object_authorization, Mapping)
    route_manifest_source = object_authorization["routeManifestSource"]
    assert isinstance(route_manifest_source, str)
    paths = [route_manifest_source]
    for selector in manifest_selectors(manifest):
        path = _selector_source_path(selector, owner="stable security-gate selector")
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _junit_counts(junit_bytes: bytes, selectors: Sequence[str]) -> dict[str, int]:
    if type(junit_bytes) is not bytes or len(junit_bytes) > JUNIT_MAX_BYTES:
        raise StableSecurityGateError("stable security-gate JUnit is oversized")
    if b"<!DOCTYPE" in junit_bytes or b"<!ENTITY" in junit_bytes:
        raise StableSecurityGateError(
            "stable security-gate JUnit must not contain document type declarations"
        )
    try:
        root = ElementTree.fromstring(junit_bytes)
    except ElementTree.ParseError as error:
        raise StableSecurityGateError(
            "stable security-gate JUnit is not valid XML"
        ) from error

    observed: dict[str, str] = {}
    for testcase in root.iter():
        if testcase.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        classname = testcase.attrib.get("classname")
        name = testcase.attrib.get("name")
        if not classname or not name:
            raise StableSecurityGateError(
                "stable security-gate JUnit testcase identity is incomplete"
            )
        node_id = f"{classname.replace('.', '/')}.py::{name}"
        if node_id in observed:
            raise StableSecurityGateError(
                "stable security-gate JUnit contains duplicate testcase identities"
            )
        status_name = "passed"
        for child in testcase:
            child_name = child.tag.rsplit("}", 1)[-1]
            if child_name in {"failure", "error", "skipped"}:
                status_name = child_name
                break
        observed[node_id] = status_name

    expected = list(dict.fromkeys(selectors))
    if list(observed) != expected:
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        raise StableSecurityGateError(
            "stable security-gate JUnit does not match the exact selector manifest "
            f"(missing={missing}, unexpected={unexpected})"
        )
    non_passing = {
        node_id: status_name
        for node_id, status_name in observed.items()
        if status_name != "passed"
    }
    if non_passing:
        raise StableSecurityGateError(
            f"stable security-gate JUnit has non-passing selectors: {non_passing}"
        )
    return {
        "collected": len(expected),
        "passed": len(expected),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


def _category_results(
    categories: Sequence[Mapping[str, object]],
    source_blobs: Mapping[str, bytes],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for category in categories:
        category_id = category["id"]
        selectors = category["pytestSelectors"]
        assert isinstance(category_id, str)
        assert isinstance(selectors, Sequence)
        source_paths: list[str] = []
        for selector in selectors:
            path = _selector_source_path(
                selector,
                owner=f"stable security-gate category {category_id}",
            )
            if path not in source_paths:
                source_paths.append(path)
        results.append(
            {
                "id": category_id,
                "pytestSelectors": list(selectors),
                "sourceDigests": [
                    {"path": path, "sha256": _sha256(source_blobs[path])}
                    for path in source_paths
                ],
            }
        )
    return results


def build_result(
    *,
    manifest: Mapping[str, object],
    manifest_bytes: bytes,
    candidate_commit: str,
    source_blobs: Mapping[str, bytes],
    junit_bytes: bytes,
    artifact_name: str,
) -> dict[str, object]:
    if GIT_COMMIT.fullmatch(candidate_commit) is None:
        raise StableSecurityGateError("stable security-gate commit is invalid")
    if type(artifact_name) is not str or ARTIFACT_NAME.fullmatch(artifact_name) is None:
        raise StableSecurityGateError(
            "stable security-gate artifact name is not attempt-scoped"
        )
    expected_source_paths = manifest_source_paths(manifest)
    if set(source_blobs) != set(expected_source_paths) or any(
        type(source_blobs[path]) is not bytes for path in expected_source_paths
    ):
        raise StableSecurityGateError(
            "stable security-gate source evidence is incomplete"
        )
    selectors = manifest_selectors(manifest)
    counts = _junit_counts(junit_bytes, selectors)
    object_authorization = manifest["objectAuthorization"]
    adversarial_resources = manifest["adversarialResources"]
    runner = manifest["runner"]
    artifact = manifest["artifact"]
    assert isinstance(object_authorization, Mapping)
    assert isinstance(adversarial_resources, Mapping)
    assert isinstance(runner, Mapping)
    assert isinstance(artifact, Mapping)
    route_manifest_path = object_authorization["routeManifestSource"]
    assert isinstance(route_manifest_path, str)
    object_categories = object_authorization["categories"]
    adversarial_categories = adversarial_resources["categories"]
    assert isinstance(object_categories, Sequence)
    assert isinstance(adversarial_categories, Sequence)
    payload: dict[str, object] = {
        "formatVersion": manifest["resultFormatVersion"],
        "candidateCommit": candidate_commit,
        "manifest": {"path": MANIFEST_PATH, "sha256": _sha256(manifest_bytes)},
        "runner": dict(runner),
        "pytest": {
            "status": "passed",
            "exitCode": 0,
            **counts,
            "junit": {
                "artifactName": artifact_name,
                "path": artifact["junitFile"],
                "sha256": _sha256(junit_bytes),
            },
        },
        "objectAuthorization": {
            "status": "passed",
            "scope": [category["id"] for category in object_categories],
            "routeManifest": {
                "path": route_manifest_path,
                "sha256": _sha256(source_blobs[route_manifest_path]),
            },
            "categories": _category_results(object_categories, source_blobs),
        },
        "adversarialResources": {
            "status": "passed",
            "categories": _category_results(adversarial_categories, source_blobs),
        },
    }
    payload["resultDigest"] = canonical_hash(payload)
    return payload


def validate_result(
    value: object,
    *,
    manifest: Mapping[str, object],
    manifest_bytes: bytes,
    candidate_commit: str,
    source_blobs: Mapping[str, bytes],
    junit_bytes: bytes | None = None,
    expected_artifact_name: str | None = None,
) -> dict[str, object]:
    result = _require_exact_mapping(
        value,
        {
            "formatVersion",
            "candidateCommit",
            "manifest",
            "runner",
            "pytest",
            "objectAuthorization",
            "adversarialResources",
            "resultDigest",
        },
        owner="stable security-gate result",
    )
    expected_source_paths = manifest_source_paths(manifest)
    if set(source_blobs) != set(expected_source_paths) or any(
        type(source_blobs[path]) is not bytes for path in expected_source_paths
    ):
        raise StableSecurityGateError(
            "stable security-gate source evidence is incomplete"
        )
    if (
        type(result.get("formatVersion")) is not int
        or result.get("formatVersion") != manifest["resultFormatVersion"]
        or result.get("candidateCommit") != candidate_commit
    ):
        raise StableSecurityGateError(
            "stable security-gate result does not bind the candidate commit"
        )
    manifest_record = _require_exact_mapping(
        result.get("manifest"),
        {"path", "sha256"},
        owner="stable security-gate result manifest",
    )
    if manifest_record != {
        "path": MANIFEST_PATH,
        "sha256": _sha256(manifest_bytes),
    }:
        raise StableSecurityGateError(
            "stable security-gate result does not bind the exact manifest"
        )
    runner = _require_exact_mapping(
        result.get("runner"),
        {"os", "python"},
        owner="stable security-gate result runner",
    )
    if runner != manifest["runner"]:
        raise StableSecurityGateError(
            "stable security-gate result does not bind the canonical runner"
        )

    selectors = manifest_selectors(manifest)
    pytest_result = _require_exact_mapping(
        result.get("pytest"),
        {
            "status",
            "exitCode",
            "collected",
            "passed",
            "failed",
            "errors",
            "skipped",
            "junit",
        },
        owner="stable security-gate pytest result",
    )
    expected_counts = {
        "status": "passed",
        "exitCode": 0,
        "collected": len(selectors),
        "passed": len(selectors),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    if any(
        type(pytest_result.get(key)) is not type(value)
        or pytest_result.get(key) != value
        for key, value in expected_counts.items()
    ):
        raise StableSecurityGateError(
            "stable security-gate pytest result is not an exact all-pass run"
        )
    junit = _require_exact_mapping(
        pytest_result.get("junit"),
        {"artifactName", "path", "sha256"},
        owner="stable security-gate JUnit evidence",
    )
    artifact = manifest["artifact"]
    assert isinstance(artifact, Mapping)
    junit_digest = junit.get("sha256")
    artifact_name = junit.get("artifactName")
    if (
        type(artifact_name) is not str
        or ARTIFACT_NAME.fullmatch(artifact_name) is None
        or (
            expected_artifact_name is not None
            and artifact_name != expected_artifact_name
        )
        or junit.get("path") != artifact["junitFile"]
        or type(junit_digest) is not str
        or SHA256.fullmatch(junit_digest) is None
    ):
        raise StableSecurityGateError(
            "stable security-gate JUnit evidence identity is invalid"
        )
    if junit_bytes is not None:
        counts = _junit_counts(junit_bytes, selectors)
        if junit_digest != _sha256(junit_bytes) or any(
            pytest_result.get(key) != value for key, value in counts.items()
        ):
            raise StableSecurityGateError(
                "stable security-gate result does not bind the supplied JUnit"
            )

    object_authorization = manifest["objectAuthorization"]
    adversarial_resources = manifest["adversarialResources"]
    assert isinstance(object_authorization, Mapping)
    assert isinstance(adversarial_resources, Mapping)
    object_categories = object_authorization["categories"]
    adversarial_categories = adversarial_resources["categories"]
    assert isinstance(object_categories, Sequence)
    assert isinstance(adversarial_categories, Sequence)
    route_manifest_path = object_authorization["routeManifestSource"]
    assert isinstance(route_manifest_path, str)
    expected_object_authorization = {
        "status": "passed",
        "scope": [category["id"] for category in object_categories],
        "routeManifest": {
            "path": route_manifest_path,
            "sha256": _sha256(source_blobs[route_manifest_path]),
        },
        "categories": _category_results(object_categories, source_blobs),
    }
    expected_adversarial_resources = {
        "status": "passed",
        "categories": _category_results(adversarial_categories, source_blobs),
    }
    if result.get("objectAuthorization") != expected_object_authorization:
        raise StableSecurityGateError(
            "stable security-gate object-authorization evidence is incomplete"
        )
    if result.get("adversarialResources") != expected_adversarial_resources:
        raise StableSecurityGateError(
            "stable security-gate adversarial-resource evidence is incomplete"
        )

    normalized: dict[str, object] = {
        "formatVersion": manifest["resultFormatVersion"],
        "candidateCommit": candidate_commit,
        "manifest": dict(manifest_record),
        "runner": dict(runner),
        "pytest": {
            **expected_counts,
            "junit": dict(junit),
        },
        "objectAuthorization": expected_object_authorization,
        "adversarialResources": expected_adversarial_resources,
    }
    expected_result_digest = canonical_hash(normalized)
    if result.get("resultDigest") != expected_result_digest:
        raise StableSecurityGateError(
            "stable security-gate result digest does not match its content"
        )
    normalized["resultDigest"] = expected_result_digest
    return normalized


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StableSecurityGateError(
                f"stable security-gate result repeats JSON key {key!r}"
            )
        result[key] = value
    return result


def validate_result_bytes(
    data: bytes,
    *,
    manifest: Mapping[str, object],
    manifest_bytes: bytes,
    candidate_commit: str,
    source_blobs: Mapping[str, bytes],
    junit_bytes: bytes | None = None,
    expected_artifact_name: str | None = None,
) -> dict[str, object]:
    if type(data) is not bytes or len(data) > RESULT_MAX_BYTES:
        raise StableSecurityGateError("stable security-gate result is oversized")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                StableSecurityGateError(
                    f"stable security-gate result uses invalid JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StableSecurityGateError(
            "stable security-gate result is not valid UTF-8 JSON"
        ) from error
    normalized = validate_result(
        value,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        candidate_commit=candidate_commit,
        source_blobs=source_blobs,
        junit_bytes=junit_bytes,
        expected_artifact_name=expected_artifact_name,
    )
    if data != _canonical_json_bytes(normalized):
        raise StableSecurityGateError(
            "stable security-gate result is not canonical JSON"
        )
    return normalized


def run_stable_security_gates(
    *,
    root: Path,
    manifest_path: Path,
    result_path: Path,
    junit_path: Path,
    candidate_commit: str,
    runner_os: str,
    runner_python: str,
    artifact_name: str,
) -> dict[str, object]:
    if manifest_path != root / MANIFEST_PATH:
        raise StableSecurityGateError(
            "stable security gates require the repository manifest path"
        )
    manifest, manifest_bytes = load_manifest(manifest_path)
    if {"os": runner_os, "python": runner_python} != manifest["runner"]:
        raise StableSecurityGateError(
            "stable security gates must run on the manifest-declared runner"
        )
    if ARTIFACT_NAME.fullmatch(artifact_name) is None:
        raise StableSecurityGateError(
            "stable security-gate artifact name must include the run attempt"
        )
    if result_path.exists() or result_path.is_symlink():
        raise StableSecurityGateError(
            "stable security-gate result path must not already exist"
        )
    if junit_path.exists() or junit_path.is_symlink():
        raise StableSecurityGateError(
            "stable security-gate JUnit path must not already exist"
        )
    if result_path.name != RESULT_FILE or junit_path.name != JUNIT_FILE:
        raise StableSecurityGateError(
            "stable security-gate output names must match the artifact contract"
        )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    selectors = manifest_selectors(manifest)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *selectors,
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit_path}",
        ],
        cwd=root,
        check=False,
    )
    if completed.returncode != 0:
        raise StableSecurityGateError(
            f"stable security-gate pytest exited with {completed.returncode}"
        )
    junit_bytes = _read_regular_file(
        junit_path,
        owner="stable security-gate JUnit",
        max_bytes=JUNIT_MAX_BYTES,
    )
    source_blobs = {
        path: _read_regular_file(
            root / path,
            owner=f"stable security-gate source {path}",
            max_bytes=SOURCE_MAX_BYTES,
        )
        for path in manifest_source_paths(manifest)
    }
    result = build_result(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        candidate_commit=candidate_commit,
        source_blobs=source_blobs,
        junit_bytes=junit_bytes,
        artifact_name=artifact_name,
    )
    try:
        result_path.write_bytes(_canonical_json_bytes(result))
    except OSError as error:
        raise StableSecurityGateError(
            "stable security-gate result could not be written"
        ) from error
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=ROOT / MANIFEST_PATH)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-python", required=True)
    parser.add_argument("--artifact-name", required=True)
    args = parser.parse_args(argv)
    try:
        run_stable_security_gates(
            root=args.root.resolve(),
            manifest_path=args.manifest.resolve(),
            result_path=args.result.resolve(),
            junit_path=args.junit.resolve(),
            candidate_commit=args.candidate_commit,
            runner_os=args.runner_os,
            runner_python=args.runner_python,
            artifact_name=args.artifact_name,
        )
    except StableSecurityGateError as error:
        parser.exit(2, f"stable security-gate error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
