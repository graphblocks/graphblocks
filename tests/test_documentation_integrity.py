from __future__ import annotations

from pathlib import Path
import re
import tomllib

import yaml


ROOT = Path(__file__).parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def test_documented_rust_toolchain_is_pinned_to_workspace_minimum() -> None:
    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text(encoding="utf-8"))
    rust_version = workspace["workspace"]["package"]["rust-version"]
    expected_channel = rust_version if rust_version.count(".") == 2 else f"{rust_version}.0"

    assert toolchain["toolchain"]["channel"] == expected_channel
    assert toolchain["toolchain"]["profile"] == "minimal"
    assert toolchain["toolchain"]["components"] == ["clippy", "rustfmt"]


def test_ci_enforces_documented_rust_quality_and_packaging_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    wheelhouse_gate = (ROOT / "tools" / "verify_wheelhouse.py").read_text(encoding="utf-8")

    assert "cargo fmt --all -- --check" in workflow
    assert "cargo clippy --workspace --all-targets --locked -- -D warnings" in workflow
    assert "cargo test --workspace --all-targets --locked" in workflow
    assert "cargo package" in workflow
    assert "patch_config=.cargo/config.toml" in workflow
    assert "printf '[patch.crates-io]\\n'" in workflow
    assert '>> "$patch_config"' in workflow
    assert '"${patches[@]}"' not in workflow
    assert '"--no-index"' in wheelhouse_gate
    assert '"--find-links"' in wheelhouse_gate
    assert '"check"' in wheelhouse_gate


def test_rust_packages_declare_publishable_path_versions_and_bundle_local_fixtures() -> None:
    cargo_manifests = sorted((ROOT / "crates").glob("*/Cargo.toml"))
    missing_versions: list[str] = []
    for manifest in cargo_manifests:
        for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
            if 'path = "../' in line and "version =" not in line:
                missing_versions.append(f"{manifest.relative_to(ROOT)}:{line_number}")

    assert not missing_versions, "path dependencies without publishable versions: " + ", ".join(missing_versions)

    fixture_mirrors = {
        "crates/graphblocks-compiler/tests/fixtures/compiler-cases.json": "tck/compiler/cases.json",
        "crates/graphblocks-python/src/fixtures/compiler-cases.json": "tck/compiler/cases.json",
        "crates/graphblocks-python/src/fixtures/runtime-cases.json": "tck/runtime/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/builtin-plugin.yaml": (
            "src/graphblocks/data/builtin-plugin.yaml"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/native-callback-runtime.json": (
            "tck/durable/native-callback-runtime.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/application-events-cases.json": (
            "tck/application-events/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/application-protocol-cases.json": (
            "tck/application-protocol/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/approval-review-cases.json": (
            "tck/approval-review/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/budget-race-cases.json": (
            "tck/budget-race/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/conversation-cases.json": (
            "tck/conversation/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/deployment-cases.json": (
            "tck/deployment/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/documents-cases.json": "tck/documents/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/exhaustion-cases.json": "tck/exhaustion/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/orchestration-cases.json": (
            "tck/orchestration/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/policy-cases.json": "tck/policy/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/rag-cases.json": "tck/rag/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/retry-cases.json": "tck/retry/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/runtime-cases.json": "tck/runtime/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/tool-execution-cases.json": (
            "tck/tool-execution/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/tool-lifecycle-cases.json": (
            "tck/tool-lifecycle/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/tool-result-cases.json": (
            "tck/tool-result/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/usage-cases.json": "tck/usage/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/voice-cases.json": "tck/voice/cases.json",
        "crates/graphblocks-runtime-durable/tests/fixtures/durable-cases.json": "tck/durable/cases.json",
        "crates/graphblocks-runtime-seq/tests/fixtures/sequence-cases.json": "tck/sequence/cases.json",
        "crates/graphblocks-schema/tests/fixtures/cases.json": "tck/schema/cases.json",
        "crates/graphblocks-schema/tests/fixtures/resources.json": "tck/schema/resources.json",
        "crates/graphblocks-schema/tests/fixtures/typed-values.json": "tck/schema/typed-values.json",
        "crates/graphblocks-types/tests/fixtures/typed-values.json": "tck/schema/typed-values.json",
    }
    shipped_fixtures = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "crates").glob("*/**/fixtures/*")
        if path.is_file()
    }
    assert shipped_fixtures == set(fixture_mirrors)
    for packaged_path, authoritative_path in fixture_mirrors.items():
        assert (ROOT / packaged_path).read_bytes() == (ROOT / authoritative_path).read_bytes()

    for rust_source in (ROOT / "crates").rglob("*.rs"):
        source = rust_source.read_text(encoding="utf-8")
        assert "../../../tck/" not in source
        assert 'join("../../tck/' not in source


def test_project_markdown_links_resolve() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "GOVERNANCE.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        *sorted((ROOT / "examples").rglob("*.md")),
    ]
    failures: list[str] = []

    for document in documents:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)} -> {raw_target}")

    assert not failures, "unresolved Markdown links:\n" + "\n".join(failures)


def test_living_documentation_has_one_authority_tree() -> None:
    assert not (ROOT / "docs" / "upstream").exists()
    assert (ROOT / "docs" / "specification" / "README.md").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "package-catalog.yaml").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").is_file()
    assert (ROOT / "profiles" / "policy-profiles.yaml").is_file()


def test_stable_release_matrix_is_complete_and_machine_readable() -> None:
    matrix = yaml.safe_load((ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text())
    assert matrix["matrixVersion"] == 1
    assert matrix["targetRelease"] == "1.0"
    assert matrix["currentReadiness"] == "blocked"

    tiers = {"stable", "preview", "internal", "reserved"}
    assert set(matrix["tierDefinitions"]) == tiers
    for section in ("artifacts", "profiles", "wireVersions", "integrations"):
        entries = matrix[section]
        identities = [entry["id"] for entry in entries]
        assert len(identities) == len(set(identities)), f"duplicate {section} identity"
        assert all(entry["tier"] in tiers for entry in entries)

    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    for artifact in artifacts.values():
        if path := artifact.get("path"):
            assert (ROOT / path).is_file(), f"missing release-matrix artifact path: {path}"
        if source := artifact.get("source"):
            assert source in artifacts, f"unknown release-matrix artifact source: {source}"

    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    workspace_crates = {
        f"crate:{Path(member).name}" for member in workspace["workspace"]["members"]
    }
    assert workspace_crates <= set(artifacts)

    profile_catalog = yaml.safe_load(
        (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").read_text()
    )
    catalog_profiles = {entry["id"] for entry in profile_catalog["spec"]["profiles"]}
    assert {entry["id"] for entry in matrix["profiles"]} == catalog_profiles

    stable_wires = [entry for entry in matrix["wireVersions"] if entry["tier"] == "stable"]
    assert {entry["id"] for entry in stable_wires} == {
        "graphblocks.ai/v1:Graph",
        "graphblocks.ai/v1:PluginManifest",
    }
    assert all(entry["readiness"] == "candidate-enforced" for entry in stable_wires)

    catalog = yaml.safe_load(
        (ROOT / "src" / "graphblocks" / "data" / "package-catalog.yaml").read_text()
    )
    catalog_integrations = {
        entry["name"]
        for entry in catalog["components"]
        if entry["stability"] in {"integration", "adapter"}
    }
    matrix_integrations = {entry["id"] for entry in matrix["integrations"]}
    assert catalog_integrations <= matrix_integrations

    gate_ids = [entry["id"] for entry in matrix["releaseGates"]]
    assert len(gate_ids) == len(set(gate_ids))
    referenced_gates = {
        gate
        for section in ("profiles", "wireVersions")
        for entry in matrix[section]
        for gate in entry.get("requiredGates", [])
    }
    assert referenced_gates <= set(gate_ids)

    api_gate = next(entry for entry in matrix["releaseGates"] if entry["id"] == "REL-API-SNAPSHOT")
    assert api_gate["readiness"] == "candidate-enforced"
    assert set(api_gate["blockers"]) == {"compatibility-review"}
    for evidence_path in api_gate["evidence"]:
        assert (ROOT / evidence_path).is_file(), f"missing API snapshot evidence: {evidence_path}"


def test_numeric_diagnostic_codes_have_unique_registry_entries() -> None:
    registry = yaml.safe_load(
        (ROOT / "docs" / "specification" / "reference" / "diagnostic-codes.yaml").read_text()
    )
    assert registry["registryVersion"] == 1
    pattern = re.compile(registry["codePattern"])
    status_values = set(registry["statusValues"])
    tier_values = set(registry["tierValues"])

    entries = registry["codes"]
    registered = [entry["code"] for entry in entries]
    assert len(registered) == len(set(registered))
    for entry in entries:
        assert pattern.fullmatch(entry["code"])
        assert entry["status"] in status_values
        assert entry["tier"] in tier_values
        assert entry["defaultSeverity"] in {"error", "warning", "info"}
        assert entry["meaning"].strip()

    emitted: set[str] = set()
    for root, suffix in (
        (ROOT / "src", "*.py"),
        (ROOT / "packages", "*.py"),
        (ROOT / "crates", "*.rs"),
    ):
        for source in root.rglob(suffix):
            emitted.update(re.findall(r"\bGB\d{4}\b", source.read_text(encoding="utf-8")))
    assert emitted == set(registered)

    stable_entries = [entry for entry in entries if entry["tier"] == "stable"]
    assert stable_entries
    assert all(entry["status"] == "active" for entry in stable_entries)
