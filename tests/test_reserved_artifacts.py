from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import pytest
import yaml


ROOT = Path(__file__).parents[1]
RUST_ROOT = ROOT / "crates" / "graphblocks"
NPM_ROOT = ROOT / "packages" / "graphblocks-npm"


def test_reserved_rust_crate_has_notice_only_public_surface() -> None:
    manifest = tomllib.loads((RUST_ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    package = manifest["package"]
    metadata = package["metadata"]["graphblocks"]

    assert package["name"] == "graphblocks"
    assert package["version"] == "0.0.2"
    assert package["description"] == (
        "RESERVED NAME ONLY; contains no supported GraphBlocks Rust API"
    )
    assert package["keywords"] == ["reserved-name"]
    assert "categories" not in package
    assert metadata == {
        "artifact-status": "reserved-name-only",
        "usable-api": False,
        "supported-distribution": "https://pypi.org/project/graphblocks/",
    }

    readme = (RUST_ROOT / "README.md").read_text(encoding="utf-8")
    notice = (RUST_ROOT / "RESERVED_PACKAGE_NOTICE.txt").read_text(encoding="utf-8")
    source = (RUST_ROOT / "src" / "lib.rs").read_text(encoding="utf-8")
    build = (RUST_ROOT / "build.rs").read_text(encoding="utf-8")

    assert readme.startswith("# graphblocks — RESERVED NAME ONLY\n")
    assert "Do not add this crate as a dependency" in readme
    assert notice.startswith("RESERVED PACKAGE:")
    assert "contains no supported Rust API" in notice
    assert re.findall(
        r"^pub (?:const|fn|struct|enum|trait|mod) ([A-Z_a-z0-9]+)", source, re.MULTILINE
    ) == ["RESERVED_PACKAGE_NOTICE"]
    assert "pub use " not in source
    assert "VERSION" not in source
    assert 'include_str!("RESERVED_PACKAGE_NOTICE.txt")' in build
    assert 'println!("cargo:warning={notice}")' in build


def test_reserved_npm_package_fails_closed_on_import() -> None:
    manifest = json.loads((NPM_ROOT / "package.json").read_text(encoding="utf-8"))
    readme = (NPM_ROOT / "README.md").read_text(encoding="utf-8")
    source = (NPM_ROOT / "index.js").read_text(encoding="utf-8")
    smoke = (NPM_ROOT / "smoke.cjs").read_text(encoding="utf-8")

    assert manifest["name"] == "graphblocks"
    assert manifest["version"] == "0.0.2"
    assert manifest["description"] == (
        "RESERVED NAME ONLY; contains no GraphBlocks JavaScript or TypeScript API"
    )
    assert manifest["keywords"] == ["reserved-name"]
    assert manifest["main"] == "index.js"
    assert manifest["exports"] == {
        ".": "./index.js",
        "./package.json": "./package.json",
    }
    assert manifest["files"] == ["index.js", "README.md", "smoke.cjs"]
    assert manifest["scripts"] == {"test": "node smoke.cjs"}
    assert manifest["graphblocksArtifact"] == {
        "status": "reserved-name-only",
        "usableApi": False,
        "supportedDistribution": "https://pypi.org/project/graphblocks/",
    }
    assert readme.startswith("# graphblocks — RESERVED NAME ONLY\n")
    assert "Do not install this package" in readme
    assert 'error.code = "ERR_GRAPHBLOCKS_RESERVED_PACKAGE"' in source
    assert "exports.version" not in source
    assert 'require(".")' in smoke
    assert 'await import("graphblocks")' in smoke

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; CI runs the mandatory package smoke")
    completed = subprocess.run(
        [node, "smoke.cjs"],
        cwd=NPM_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


def test_reserved_artifact_release_gate_keeps_registry_state_explicit() -> None:
    matrix = yaml.safe_load(
        (ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text(
            encoding="utf-8"
        )
    )
    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    assert artifacts["crate:graphblocks"] == {
        "id": "crate:graphblocks",
        "ecosystem": "rust",
        "kind": "reserved-name-crate",
        "path": "crates/graphblocks/Cargo.toml",
        "tier": "reserved",
        "readiness": "repository-enforced-publish-pending",
        "version": "0.0.2",
        "usableApi": False,
        "publicSurface": "reserved-package-notice-only",
        "buildBehavior": "emits-reserved-package-warning",
        "promotionGate": "REL-RESERVED-ARTIFACTS",
    }
    assert artifacts["npm:graphblocks"] == {
        "id": "npm:graphblocks",
        "ecosystem": "npm",
        "kind": "reserved-name-package",
        "path": "packages/graphblocks-npm/package.json",
        "tier": "reserved",
        "readiness": "repository-enforced-publish-pending",
        "version": "0.0.2",
        "usableApi": False,
        "importBehavior": "throws-ERR_GRAPHBLOCKS_RESERVED_PACKAGE",
        "registryDeprecationRequired": True,
        "promotionGate": "REL-RESERVED-ARTIFACTS",
    }

    gate = next(
        entry
        for entry in matrix["releaseGates"]
        if entry["id"] == "REL-RESERVED-ARTIFACTS"
    )
    assert gate["readiness"] == "repository-enforced-registry-blocked"
    assert gate["scope"] == "artifact-publication"
    assert gate["blocksTargetRelease"] is False
    assert gate["artifacts"] == ["crate:graphblocks", "npm:graphblocks"]
    assert gate["authorizationRequiredForRegistryMutation"] is True
    assert gate["requiredEvidence"] == [
        "reserved-marker-in-package-metadata-and-readme",
        "rust-build-warning-and-notice-only-public-surface",
        "npm-import-dedicated-reserved-package-error",
        "package-content-and-smoke-gates",
        "published-reserved-marker-revisions",
        "npm-registry-deprecation-message",
    ]
    assert gate["blockers"] == [
        "crates-io-graphblocks-0.0.2-not-published",
        "npm-graphblocks-0.0.2-not-published",
        "npm-registry-deprecation-message-not-applied",
    ]
    assert gate["id"] not in matrix["globalRequiredGates"]
    assert all(
        gate["id"] not in profile["requiredGates"] for profile in matrix["profiles"]
    )
    assert all((ROOT / path).is_file() for path in gate["currentEvidence"])


def test_ci_runs_reserved_package_content_and_smoke_gates() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = {step["name"]: step for step in workflow["jobs"]["python"]["steps"]}
    rust_step = steps["Verify reserved Rust artifact boundary"]
    npm_step = steps["Verify reserved npm artifact boundary"]
    condition = "${{ matrix.os == 'ubuntu-latest' && matrix.python-version == '3.11' }}"

    assert rust_step["if"] == condition
    assert rust_step["run"].splitlines() == [
        "cargo test -p graphblocks --locked",
        "cargo package -p graphblocks --list --locked",
    ]
    assert npm_step["if"] == condition
    assert npm_step["working-directory"] == "packages/graphblocks-npm"
    assert npm_step["run"].splitlines() == [
        "node --version",
        "npm test",
        "npm pack --dry-run --json",
    ]
