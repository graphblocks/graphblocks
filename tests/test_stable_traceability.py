from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).parents[1]
TRACEABILITY_PATH = ROOT / "docs" / "project" / "stable-requirements.yaml"
PROFILE_CATALOG_PATH = ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"
STABLE_PROFILES = ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME")
REQUIREMENT_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CLAUSE_ID = re.compile(r"GB-[A-Z0-9]+(?:-[A-Z0-9]+)+")
ANCHOR = re.compile(r'<a id="(?P<id>GB-[A-Z0-9]+(?:-[A-Z0-9]+)+)"></a>')
ANCHOR_BLOCK = re.compile(r'^<a id="(?P<id>GB-[A-Z0-9]+(?:-[A-Z0-9]+)+)"></a>$')
HEADING = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*#*$")
NORMATIVE = re.compile(r"\bMUST(?:\s+NOT)?\b")


def _load_mapping(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _markdown_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    lines: list[str] = []
    start = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not lines:
                start = line_number
            lines.append(line)
        elif lines:
            blocks.append((start, "\n".join(lines)))
            lines = []
    if lines:
        blocks.append((start, "\n".join(lines)))
    return blocks


def _stable_clause_inventory(
    normative_sections: dict[str, object],
) -> dict[str, tuple[str, int]]:
    inventory: dict[str, tuple[str, int]] = {}
    anchor_locations: dict[str, list[tuple[str, int]]] = {}

    for relative_path, section_value in normative_sections.items():
        assert isinstance(relative_path, str)
        assert isinstance(section_value, list) and section_value
        sections = section_value
        assert all(isinstance(section, str) and section for section in sections)
        assert len(sections) == len(set(sections)), f"duplicate selected section in {relative_path}"

        path = ROOT / relative_path
        assert path.is_file(), f"missing normative source {relative_path}"
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for match in ANCHOR.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            anchor_locations.setdefault(match.group("id"), []).append(
                (relative_path, line_number)
            )

        heading_counts: Counter[str] = Counter()
        for line in lines:
            match = HEADING.fullmatch(line)
            if match:
                heading_counts[match.group("title")] += 1
        for section in sections:
            assert heading_counts[section] == 1, (
                f"selected section {section!r} occurs {heading_counts[section]} times "
                f"in {relative_path}"
            )

        selected = set(sections)
        current_section: str | None = None
        pending_anchor: tuple[str, int] | None = None
        selected_clause_count = 0
        for line_number, block in _markdown_blocks(text):
            heading = HEADING.fullmatch(block)
            if heading:
                assert pending_anchor is None, (
                    f"orphan clause anchor {pending_anchor} before {relative_path}:{line_number}"
                )
                current_section = heading.group("title")
                continue

            anchor = ANCHOR_BLOCK.fullmatch(block)
            if anchor:
                assert pending_anchor is None, (
                    f"adjacent clause anchors at {relative_path}:{line_number}"
                )
                pending_anchor = (anchor.group("id"), line_number)
                continue

            if current_section in selected and NORMATIVE.search(block):
                assert pending_anchor is not None, (
                    f"stable normative paragraph lacks a clause anchor at "
                    f"{relative_path}:{line_number}"
                )
                clause_id, anchor_line = pending_anchor
                assert CLAUSE_ID.fullmatch(clause_id)
                assert clause_id not in inventory, f"duplicate stable clause ID {clause_id}"
                inventory[clause_id] = (relative_path, anchor_line)
                selected_clause_count += 1
                pending_anchor = None
                continue

            assert pending_anchor is None, (
                f"clause anchor at {relative_path}:{pending_anchor[1]} is not tied to a "
                "selected MUST/MUST NOT paragraph"
            )

        assert pending_anchor is None, f"orphan trailing clause anchor in {relative_path}"
        assert selected_clause_count, f"no stable clauses found in {relative_path}"

    for clause_id, locations in anchor_locations.items():
        assert len(locations) == 1, f"duplicate clause anchor {clause_id}: {locations}"
        assert clause_id in inventory, (
            f"clause anchor {clause_id} at {locations[0]} is outside selected stable sections"
        )
    return inventory


def test_stable_requirement_traceability_matches_the_profile_catalog() -> None:
    traceability = _load_mapping(TRACEABILITY_PATH)
    configured_catalog = (
        TRACEABILITY_PATH.parent / str(traceability["profileCatalog"])
    ).resolve()
    assert configured_catalog == PROFILE_CATALOG_PATH.resolve()

    catalog = _load_mapping(configured_catalog)
    profiles = traceability["profiles"]
    assert isinstance(profiles, dict)
    catalog_profiles = {
        profile["id"]: profile
        for profile in catalog["spec"]["profiles"]  # type: ignore[index]
    }

    assert traceability["traceabilityVersion"] == 3
    assert traceability["targetRelease"] == "1.0"
    assert set(profiles) == set(STABLE_PROFILES)
    for profile_id in STABLE_PROFILES:
        profile = profiles[profile_id]
        assert isinstance(profile, dict)
        catalog_profile = catalog_profiles[profile_id]
        assert profile["status"] == catalog_profile["status"]
        assert profile["extends"] == catalog_profile.get("extends", [])

        entries = profile["requirements"]
        assert isinstance(entries, list) and entries
        requirement_ids = [entry["id"] for entry in entries]
        assert requirement_ids == catalog_profile["requires"]
        assert len(requirement_ids) == len(set(requirement_ids))
        assert all(REQUIREMENT_ID.fullmatch(requirement_id) for requirement_id in requirement_ids)
        covered_tck = {suite for entry in entries for suite in entry["tckSuites"]}
        inherited_tck = {
            suite
            for parent_id in catalog_profile.get("extends", [])
            for suite in catalog_profiles[parent_id]["tck"]
        }
        assert covered_tck - inherited_tck == set(catalog_profile["tck"])
        for suite in covered_tck:
            assert (ROOT / "tck" / suite).is_dir(), f"missing TCK suite {suite}"


def test_every_stable_clause_has_exact_requirement_and_evidence_traceability() -> None:
    traceability = _load_mapping(TRACEABILITY_PATH)
    normative_sections = traceability["normativeSections"]
    assert isinstance(normative_sections, dict) and normative_sections
    inventory = _stable_clause_inventory(normative_sections)

    profiles = traceability["profiles"]
    assert isinstance(profiles, dict)
    claimed_clauses: set[str] = set()
    for profile_id, profile in profiles.items():
        assert isinstance(profile_id, str)
        assert isinstance(profile, dict)
        entries = profile["requirements"]
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            requirement = f"{profile_id}/{entry['id']}"
            clauses = entry["clauses"]
            assert isinstance(clauses, list) and clauses, f"{requirement} clauses"
            assert len(clauses) == len(set(clauses)), f"{requirement} duplicate clauses"
            for clause_id in clauses:
                assert isinstance(clause_id, str) and CLAUSE_ID.fullmatch(clause_id)
                assert clause_id in inventory, f"{requirement} missing clause {clause_id}"
                claimed_clauses.add(clause_id)

            implementations = entry["implementations"]
            assert isinstance(implementations, dict) and implementations, (
                f"{requirement} implementations"
            )
            assert set(implementations) <= {
                "normative",
                "pythonFacadeAndReference",
                "targetNormative",
            }
            assert "normative" in implementations or "targetNormative" in implementations
            implementation_paths: list[str] = []
            for role, paths in implementations.items():
                assert isinstance(role, str)
                assert isinstance(paths, list) and paths, (
                    f"{requirement} {role} implementations"
                )
                for relative_path in paths:
                    assert isinstance(relative_path, str)
                    assert (ROOT / relative_path).is_file(), (
                        f"{requirement} missing {relative_path}"
                    )
                    implementation_paths.append(relative_path)
            assert len(implementation_paths) == len(set(implementation_paths)), (
                f"{requirement} implementation path has multiple roles"
            )

            paths = entry["evidence"]
            assert isinstance(paths, list) and paths, f"{requirement} evidence"
            for relative_path in paths:
                assert isinstance(relative_path, str)
                assert (ROOT / relative_path).is_file(), (
                    f"{requirement} missing {relative_path}"
                )

            schemas = entry["schemas"]
            tck_suites = entry["tckSuites"]
            assert isinstance(schemas, list)
            assert isinstance(tck_suites, list)
            for relative_path in schemas:
                assert isinstance(relative_path, str)
                assert (ROOT / relative_path).is_file(), (
                    f"{requirement} missing {relative_path}"
                )
            assert schemas or tck_suites or any(
                str(path).startswith("tests/test_") for path in entry["evidence"]
            ), f"{requirement} lacks schema, TCK, or focused test evidence"

    assert claimed_clauses == set(inventory), (
        f"unlisted stable clauses: {sorted(set(inventory) - claimed_clauses)}; "
        f"missing anchored clauses: {sorted(claimed_clauses - set(inventory))}"
    )


def test_stable_requirement_implementation_roles_match_the_authority_transition() -> None:
    traceability = _load_mapping(TRACEABILITY_PATH)
    assert traceability["authorityRoles"] == {
        "graphCompiler": "rust",
        "standaloneCanonicalAndSchemaIdentity": "rust",
        "standaloneResourceSchemaValidationAndMigration": "rust",
        "stableLocalRuntimeTarget": "rust",
        "python": "authoring-facade-and-explicit-reference-oracle",
        "implicitReferenceFallback": False,
    }
    assert traceability["implementationRoleDefinitions"] == {
        "normative": "Active implementation authority for the named requirement.",
        "pythonFacadeAndReference": (
            "Python authoring facade, deterministic reference, or TCK-oracle "
            "implementation."
        ),
        "targetNormative": (
            "Selected future normative implementation whose promotion evidence "
            "remains blocked."
        ),
    }
    profiles = traceability["profiles"]
    assert isinstance(profiles, dict)
    c0 = {
        entry["id"]: entry
        for entry in profiles["GB-C0-SCHEMA"]["requirements"]
    }
    compiler_roles = c0["graph-parse-normalize-hash"]["implementations"]
    assert set(compiler_roles) == {"normative", "pythonFacadeAndReference"}
    assert all(
        not path.startswith("src/graphblocks/")
        for path in compiler_roles["normative"]
    )
    assert "targetNormative" in c0["canonical-schema-roundtrip"]["implementations"]
    authority_slices = c0["canonical-schema-roundtrip"]["authoritySlices"]
    assert authority_slices["canonicalAndSchemaIdentity"] == {
        "status": "normative",
        "authority": "rust",
        "normativeImplementations": [
            "crates/graphblocks-schema/src/lib.rs",
            "crates/graphblocks-compiler/src/canonical.rs",
        ],
        "pythonFacadeAndReference": [
            "src/graphblocks/canonical.py",
            "src/graphblocks/schema.py",
        ],
        "installedEvidence": [
            "tools/verify_wheelhouse.py",
            "tools/release_supply_chain.py",
            "tests/test_native_canonical_differential.py",
            "tests/test_native_schema_id_differential.py",
            "tests/test_wheelhouse_schema_verification.py",
            "tests/test_release_supply_chain.py",
        ],
    }
    assert authority_slices["resourceSchemaValidationAndMigration"] == {
        "status": "normative",
        "authority": "rust",
        "normativeImplementations": [
            "crates/graphblocks-schema/src/lib.rs",
            "crates/graphblocks-python/src/lib.rs",
        ],
        "pythonFacadeAndReference": [
            "src/graphblocks/schema.py",
            "src/graphblocks/migration.py",
        ],
        "installedEvidence": [
            "tools/verify_wheelhouse.py",
            "tools/release_supply_chain.py",
            "tests/test_native_resource_schema_differential.py",
            "tests/test_native_resource_migration_differential.py",
            "tests/test_wheelhouse_schema_verification.py",
            "tests/test_release_supply_chain.py",
        ],
    }
    plugin_roles = c0["plugin-manifest-validation"]["implementations"]
    assert set(plugin_roles) == {"normative", "pythonFacadeAndReference"}
    assert "crates/graphblocks-schema/src/lib.rs" in plugin_roles["normative"]
    migration = c0["migration-reader"]
    migration_roles = migration["implementations"]
    assert set(migration_roles) == {"normative", "pythonFacadeAndReference"}
    assert "crates/graphblocks-schema/src/lib.rs" in migration_roles["normative"]
    assert migration["scopeNotes"] == [
        "Graph compiler migration and standalone Graph/PluginManifest migration route "
        "through fail-closed Rust authority; explicit Python reference entry points "
        "remain available as the oracle."
    ]

    c1_entries = {
        entry["id"]: entry
        for entry in profiles["GB-C1-LOCAL-RUNTIME"]["requirements"]
    }
    normative_requirements = {
        "deterministic-local-scheduler",
        "journal",
        "local-runtime-api",
        "typed-ports",
    }
    target_requirements = {
        "outcome",
        "cancellation",
        "local-flow",
    }
    assert set(c1_entries) == normative_requirements | target_requirements
    for requirement in normative_requirements:
        roles = c1_entries[requirement]["implementations"]
        assert set(roles) == {"normative", "pythonFacadeAndReference"}
        assert all(
            not path.startswith("src/graphblocks/")
            for path in roles["normative"]
        )
    assert "compatibility/stable-runtime-api.json" in c1_entries[
        "local-runtime-api"
    ]["evidence"]
    typed_ports = c1_entries["typed-ports"]
    assert "crates/graphblocks-runtime-core/src/typed_value.rs" not in typed_ports[
        "implementations"
    ]["normative"]
    assert typed_ports["scopeNotes"] == [
        "The normative slice combines the installed compiler corpus for nominal "
        "identity, requiredness, and nested-root validation with the typed-ports "
        "corpus for portable typed Graph authoring and exact local-runtime port "
        "preservation. "
        "crates/graphblocks-runtime-core/src/typed_value.rs remains a preview "
        "implementation detail outside this authority claim."
    ]
    assert typed_ports["tckSuites"] == ["compiler", "runtime", "typed-ports"]
    assert typed_ports["authoritySlices"][
        "typedPortCompilationAndLocalRuntime"
    ] == {
        "status": "normative",
        "authority": "rust",
        "scope": (
            "nominal-port-identity-requiredness-nested-root-validation-and-"
            "local-runtime-port-preservation"
        ),
        "normativeImplementations": [
            "crates/graphblocks-compiler/src/compiler.rs",
            "crates/graphblocks-runtime-core/src/typed_graph.rs",
            "crates/graphblocks-python/src/lib.rs",
            "crates/graphblocks-python/src/typed_ports_tck.rs",
        ],
        "pythonFacadeAndReference": [
            "src/graphblocks/compiler.py",
            "src/graphblocks/plugins.py",
            "src/graphblocks/runtime.py",
            "src/graphblocks/typed.py",
        ],
        "installedEvidence": [
            "packages/graphblocks-runtime/src/graphblocks_runtime/__init__.py",
            "packages/graphblocks-testing/src/graphblocks_testing/runners.py",
            "packages/graphblocks-testing/src/graphblocks_testing/cli.py",
            "tools/verify_wheelhouse.py",
            "tests/test_testing_package.py",
            "tests/test_wheelhouse_schema_verification.py",
            "tests/test_release_supply_chain.py",
        ],
        "exclusions": [
            "Generic typed-value transport and remote-boundary policy remain "
            "preview and are not promoted by this slice."
        ],
    }
    application_event_slice = c1_entries["outcome"]["authoritySlices"][
        "applicationEventStreamAdmission"
    ]
    assert application_event_slice["authority"] == "rust"
    assert application_event_slice["scope"] == (
        "normalized-raw-construction-operation-trace-structured-diagnostics-"
        "plus-materialized-admission-and-projection"
    )
    assert application_event_slice["exclusions"] == []
    for requirement in target_requirements:
        entry = c1_entries[requirement]
        roles = entry["implementations"]
        assert "normative" not in roles, entry["id"]
        assert set(roles) == {"pythonFacadeAndReference", "targetNormative"}
