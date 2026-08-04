from __future__ import annotations

import ast
import json
from pathlib import Path
import tomllib

from packaging.specifiers import SpecifierSet
import yaml

from graphblocks.packages import load_package_catalog


ROOT = Path(__file__).parents[1]
MATRIX_PATH = ROOT / "docs" / "project" / "version-compatibility.yaml"


def _load_matrix() -> dict[str, object]:
    matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    assert isinstance(matrix, dict)
    return matrix


def test_artifact_versions_and_catalog_constraints_match_published_matrix() -> None:
    matrix = _load_matrix()
    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    assert set(artifacts) == {
        "pypi:graphblocks",
        "pypi:graphblocks-testing",
        "pypi:graphblocks-runtime",
        "helm:graphblocks-deployment-chart",
        "cargo:active-workspace",
        "cargo:graphblocks-reserved",
        "npm:graphblocks-reserved",
    }

    for artifact in artifacts.values():
        expected = artifact["version"]
        if "manifests" in artifact:
            observed = {
                tomllib.loads((ROOT / path).read_text(encoding="utf-8"))["package"][
                    "version"
                ]
                for path in artifact["manifests"]
            }
            assert observed == {expected}
            continue
        manifest = ROOT / artifact["manifest"]
        if manifest.name == "package.json":
            observed = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        elif manifest.name == "Chart.yaml":
            observed = yaml.safe_load(manifest.read_text(encoding="utf-8"))["version"]
        else:
            owner = (
                "project"
                if manifest.suffix == ".toml" and "pyproject" in manifest.name
                else "package"
            )
            observed = tomllib.loads(manifest.read_text(encoding="utf-8"))[owner][
                "version"
            ]
        assert observed == expected

    catalog = load_package_catalog()
    assert catalog["catalogVersion"] == 9
    catalog_artifacts = {entry["distribution"]: entry for entry in catalog["artifacts"]}
    for artifact in artifacts.values():
        distribution = artifact.get("catalogDistribution")
        if distribution is None:
            continue
        constraint = catalog_artifacts[distribution]["versionConstraint"]
        assert SpecifierSet(constraint).contains(
            artifact["version"],
            prereleases=True,
        )


def test_contract_versions_are_independent_and_fail_closed_on_mismatch() -> None:
    matrix = _load_matrix()
    assert matrix["policy"] == {
        "packageSemverEqualsContractVersion": False,
        "compatibilityAuthority": "this-matrix-plus-runtime-handshake",
        "unsupportedCombinationBehavior": "fail-closed-before-operation",
        "releaseTrainMeaning": (
            "Package versions identify independently published artifacts; protocol, "
            "schema, and checkpoint versions identify interoperability contracts."
        ),
    }
    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    contracts = {entry["id"]: entry for entry in matrix["contracts"]}
    assert set(contracts) == {
        "schema-graph-v1",
        "native-binding-v1",
        "worker-protocol-v1",
        "application-protocol-v1",
        "durable-checkpoint-v1",
    }

    expected_python_constants = {
        "packages/graphblocks-runtime/src/graphblocks_runtime/__init__.py": {
            "_NATIVE_BINDING_PROTOCOL_VERSION": 1,
        },
        "src/graphblocks/worker.py": {"WORKER_PROTOCOL_VERSION": 1},
        "src/graphblocks/durable_server.py": {
            "DURABLE_RUNTIME_FORMAT_VERSION": "graphblocks.runtime@v1",
        },
    }
    for relative_path, expected_assignments in expected_python_constants.items():
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        observed: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in expected_assignments:
                    observed[target.id] = ast.literal_eval(node.value)
        assert observed == expected_assignments

    rust_protocol = (ROOT / "crates/graphblocks-protocol/src/lib.rs").read_text(
        encoding="utf-8"
    )
    assert "pub const WORKER_PROTOCOL_VERSION: u16 = 1;" in rust_protocol
    assert contracts["native-binding-v1"]["version"] == 1
    assert contracts["worker-protocol-v1"]["version"] == 1
    assert contracts["application-protocol-v1"]["version"] == "graphblocks.app.v1"
    assert contracts["durable-checkpoint-v1"]["version"] == "graphblocks.runtime@v1"

    for contract in contracts.values():
        assert (ROOT / contract["authority"]).is_file()
        if "mirror" in contract:
            assert (ROOT / contract["mirror"]).is_file()
        for combination in contract["combinations"]:
            artifact = artifacts[combination["artifact"]]
            assert SpecifierSet(combination["versions"]).contains(
                artifact["version"],
                prereleases=True,
            )
        assert contract["mismatchEvidence"]
        assert all((ROOT / path).is_file() for path in contract["mismatchEvidence"])
