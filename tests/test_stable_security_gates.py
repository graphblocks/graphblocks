from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ElementTree

import pytest
import yaml

from graphblocks.canonical import canonical_dumps
from tools import stable_security_gates


ROOT = Path(__file__).parents[1]
MATRIX_PATH = ROOT / "docs" / "project" / "stable-release-matrix.yaml"
COMMIT = "a" * 40


def _junit_bytes(
    selectors: tuple[str, ...],
    *,
    skipped_selector: str | None = None,
) -> bytes:
    suites = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        suites,
        "testsuite",
        {
            "tests": str(len(selectors)),
            "failures": "0",
            "errors": "0",
            "skipped": "1" if skipped_selector is not None else "0",
        },
    )
    for selector in selectors:
        path, name = selector.split("::", 1)
        testcase = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": path.removesuffix(".py").replace("/", "."),
                "name": name,
            },
        )
        if selector == skipped_selector:
            ElementTree.SubElement(testcase, "skipped")
    return ElementTree.tostring(suites, encoding="utf-8", xml_declaration=True)


def _result_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    bytes,
    dict[str, bytes],
    bytes,
]:
    manifest, manifest_bytes = stable_security_gates.load_manifest()
    source_blobs = {
        path: f"candidate source: {path}\n".encode()
        for path in stable_security_gates.manifest_source_paths(manifest)
    }
    junit_bytes = _junit_bytes(stable_security_gates.manifest_selectors(manifest))
    result = stable_security_gates.build_result(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        candidate_commit=COMMIT,
        source_blobs=source_blobs,
        junit_bytes=junit_bytes,
        artifact_name=f"{stable_security_gates.ARTIFACT_NAME_PREFIX}-1",
    )
    return result, manifest, manifest_bytes, source_blobs, junit_bytes


def test_stable_security_gate_manifest_matches_release_claims() -> None:
    manifest, _manifest_bytes = stable_security_gates.load_manifest()
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    release_gates = {gate["id"]: gate for gate in matrix["releaseGates"]}
    object_categories = manifest["objectAuthorization"]["categories"]
    adversarial_categories = manifest["adversarialResources"]["categories"]

    assert [category["id"] for category in object_categories] == release_gates[
        "REL-OBJECT-AUTHORIZATION-REVIEW"
    ]["scope"]
    assert [category["id"] for category in adversarial_categories] == release_gates[
        "REL-ADVERSARIAL-RESOURCE-TESTS"
    ]["categories"]
    assert manifest["artifact"] == {
        "namePrefix": "graphblocks-stable-security-gates",
        "resultFile": "stable-security-gates.json",
        "junitFile": "stable-security-gates.xml",
    }
    assert manifest["runner"] == {"os": "ubuntu-latest", "python": "3.11"}

    selectors = stable_security_gates.manifest_selectors(manifest)
    assert (
        "tests/test_server_core.py::"
        "test_server_app_hides_foreign_callback_delivery_from_control_request[redrive]"
    ) in selectors
    assert (
        "tests/test_server_core.py::"
        "test_server_app_hides_foreign_callback_delivery_from_control_request[dead-letter]"
    ) in selectors
    assert (
        "tests/test_canonical_properties.py::"
        "test_canonical_integer_token_budget_holds_near_the_boundary"
    ) in selectors
    assert (
        "tests/test_canonical_numeric_encoding.py::"
        "test_canonical_numeric_tokens_do_not_rescan_output_per_value"
    ) in selectors


def test_stable_security_gate_result_binds_manifest_sources_and_junit() -> None:
    result, manifest, manifest_bytes, source_blobs, junit_bytes = _result_fixture()

    assert (
        stable_security_gates.validate_result(
            result,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            candidate_commit=COMMIT,
            source_blobs=source_blobs,
            junit_bytes=junit_bytes,
        )
        == result
    )
    result_bytes = (canonical_dumps(result) + "\n").encode()
    assert (
        stable_security_gates.validate_result_bytes(
            result_bytes,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            candidate_commit=COMMIT,
            source_blobs=source_blobs,
            junit_bytes=junit_bytes,
        )
        == result
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "candidate-commit",
        "manifest-digest",
        "source-digest",
        "junit-digest",
        "pass-count",
        "boolean-exit-code",
        "category-selector",
        "result-digest",
        "unknown-field",
    ),
)
def test_stable_security_gate_result_rejects_substitution(mutation: str) -> None:
    result, manifest, manifest_bytes, source_blobs, junit_bytes = _result_fixture()
    changed = deepcopy(result)
    if mutation == "candidate-commit":
        changed["candidateCommit"] = "b" * 40
    elif mutation == "manifest-digest":
        changed["manifest"]["sha256"] = "sha256:" + "1" * 64
    elif mutation == "source-digest":
        changed["objectAuthorization"]["categories"][0]["sourceDigests"][0][
            "sha256"
        ] = "sha256:" + "2" * 64
    elif mutation == "junit-digest":
        changed["pytest"]["junit"]["sha256"] = "sha256:" + "3" * 64
    elif mutation == "pass-count":
        changed["pytest"]["passed"] -= 1
    elif mutation == "boolean-exit-code":
        changed["pytest"]["exitCode"] = False
    elif mutation == "category-selector":
        changed["adversarialResources"]["categories"][0]["pytestSelectors"].pop()
    elif mutation == "result-digest":
        changed["resultDigest"] = "sha256:" + "4" * 64
    else:
        changed["approved"] = True

    with pytest.raises(stable_security_gates.StableSecurityGateError):
        stable_security_gates.validate_result(
            changed,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            candidate_commit=COMMIT,
            source_blobs=source_blobs,
            junit_bytes=junit_bytes,
        )


def test_stable_security_gate_result_rejects_noncanonical_or_duplicate_json() -> None:
    result, manifest, manifest_bytes, source_blobs, junit_bytes = _result_fixture()
    pretty = json.dumps(result, indent=2).encode()
    duplicate = pretty.replace(
        b'{\n  "formatVersion": 1,', b'{\n  "formatVersion": 1,\n  "formatVersion": 1,'
    )

    for data in (pretty, duplicate):
        with pytest.raises(stable_security_gates.StableSecurityGateError):
            stable_security_gates.validate_result_bytes(
                data,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                candidate_commit=COMMIT,
                source_blobs=source_blobs,
                junit_bytes=junit_bytes,
            )


def test_stable_security_gate_junit_requires_every_exact_selector_to_pass() -> None:
    result, manifest, manifest_bytes, source_blobs, _junit_bytes_value = (
        _result_fixture()
    )
    selectors = stable_security_gates.manifest_selectors(manifest)
    skipped = _junit_bytes(selectors, skipped_selector=selectors[0])

    with pytest.raises(
        stable_security_gates.StableSecurityGateError,
        match="non-passing selectors",
    ):
        stable_security_gates.validate_result(
            result,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            candidate_commit=COMMIT,
            source_blobs=source_blobs,
            junit_bytes=skipped,
        )


def test_stable_security_gate_manifest_rejects_unknown_fields() -> None:
    manifest = yaml.safe_load(
        (ROOT / stable_security_gates.MANIFEST_PATH).read_text(encoding="utf-8")
    )
    manifest["artifact"]["mutable"] = True

    with pytest.raises(
        stable_security_gates.StableSecurityGateError,
        match="closed shape",
    ):
        stable_security_gates.load_manifest_bytes(
            yaml.safe_dump(manifest).encode("utf-8")
        )


def test_stable_security_gate_manifest_rejects_duplicate_yaml_keys() -> None:
    manifest_bytes = (ROOT / stable_security_gates.MANIFEST_PATH).read_bytes()
    duplicate = manifest_bytes.replace(
        b"formatVersion: 1\n",
        b"formatVersion: 1\nformatVersion: 1\n",
        1,
    )

    with pytest.raises(
        stable_security_gates.StableSecurityGateError,
        match="repeats YAML key",
    ):
        stable_security_gates.load_manifest_bytes(duplicate)
