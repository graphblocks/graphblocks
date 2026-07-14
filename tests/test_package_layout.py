from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version
import pytest

import graphblocks
from graphblocks.packages import (
    PackageLock,
    PackageLockEntry,
    PackageManifestAuditPolicy,
    WheelBuildTarget,
    WheelMatrix,
    audit_package_manifests,
    build_package_lock,
    build_wheel_matrix,
    doctor_package_catalog,
    load_package_catalog,
    package_rows,
)


ROOT = Path(__file__).parents[1]

EXPECTED_COMPONENTS = {
    "graphblocks-agents",
    "graphblocks-audit",
    "graphblocks-budget",
    "graphblocks-budget-postgres",
    "graphblocks-callbacks",
    "graphblocks-cli",
    "graphblocks-client",
    "graphblocks-conversation",
    "graphblocks-core",
    "graphblocks-dashboards",
    "graphblocks-deployment",
    "graphblocks-devtools",
    "graphblocks-documents",
    "graphblocks-durable",
    "graphblocks-evaluation",
    "graphblocks-gitops",
    "graphblocks-haystack",
    "graphblocks-kafka",
    "graphblocks-kubernetes",
    "graphblocks-langfuse",
    "graphblocks-mcp",
    "graphblocks-nats",
    "graphblocks-oci",
    "graphblocks-openai",
    "graphblocks-openai-realtime",
    "graphblocks-openapi",
    "graphblocks-operator",
    "graphblocks-orchestration",
    "graphblocks-otel",
    "graphblocks-pdf",
    "graphblocks-policy",
    "graphblocks-policy-cedar",
    "graphblocks-policy-opa",
    "graphblocks-prometheus",
    "graphblocks-pubsub",
    "graphblocks-qdrant",
    "graphblocks-rag",
    "graphblocks-review",
    "graphblocks-runtime",
    "graphblocks-scripted",
    "graphblocks-server",
    "graphblocks-silero-vad",
    "graphblocks-sqs",
    "graphblocks-stdlib",
    "graphblocks-telemetry",
    "graphblocks-terraform",
    "graphblocks-testing",
    "graphblocks-tui",
    "graphblocks-usage",
    "graphblocks-usage-postgres",
    "graphblocks-voice",
    "graphblocks-webrtc",
    "graphblocks-websocket-media",
    "graphblocks-worker",
    "graphblocks-workspace",
}

INTEGRATION_COMPONENTS = {
    "graphblocks-budget-postgres",
    "graphblocks-gitops",
    "graphblocks-haystack",
    "graphblocks-kafka",
    "graphblocks-kubernetes",
    "graphblocks-langfuse",
    "graphblocks-mcp",
    "graphblocks-nats",
    "graphblocks-oci",
    "graphblocks-openai",
    "graphblocks-openai-realtime",
    "graphblocks-openapi",
    "graphblocks-otel",
    "graphblocks-pdf",
    "graphblocks-policy-cedar",
    "graphblocks-policy-opa",
    "graphblocks-prometheus",
    "graphblocks-pubsub",
    "graphblocks-qdrant",
    "graphblocks-scripted",
    "graphblocks-silero-vad",
    "graphblocks-sqs",
    "graphblocks-terraform",
    "graphblocks-usage-postgres",
    "graphblocks-webrtc",
    "graphblocks-websocket-media",
}

DIRECT_IMPORTS = {
    "graphblocks-agents": "graphblocks.agents",
    "graphblocks-audit": "graphblocks.audit",
    "graphblocks-budget": "graphblocks.budget",
    "graphblocks-callbacks": "graphblocks.callbacks",
    "graphblocks-cli": "graphblocks.cli",
    "graphblocks-client": "graphblocks.client",
    "graphblocks-conversation": "graphblocks.conversation",
    "graphblocks-core": "graphblocks",
    "graphblocks-dashboards": "graphblocks.dashboards",
    "graphblocks-deployment": "graphblocks.deployment",
    "graphblocks-devtools": "graphblocks.devtools",
    "graphblocks-documents": "graphblocks.documents",
    "graphblocks-durable": "graphblocks.durable",
    "graphblocks-evaluation": "graphblocks.evaluation",
    "graphblocks-orchestration": "graphblocks.orchestration",
    "graphblocks-policy": "graphblocks.policy",
    "graphblocks-rag": "graphblocks.rag",
    "graphblocks-review": "graphblocks.review",
    "graphblocks-runtime": "graphblocks_runtime",
    "graphblocks-server": "graphblocks.server",
    "graphblocks-stdlib": "graphblocks.stdlib",
    "graphblocks-telemetry": "graphblocks.telemetry",
    "graphblocks-testing": "graphblocks_testing",
    "graphblocks-tui": "graphblocks.tui",
    "graphblocks-usage": "graphblocks.usage",
    "graphblocks-voice": "graphblocks.voice",
    "graphblocks-worker": "graphblocks.worker",
    "graphblocks-workspace": "graphblocks.workspace",
}

DEFAULT_COMPONENTS = {
    "graphblocks-budget",
    "graphblocks-cli",
    "graphblocks-conversation",
    "graphblocks-core",
    "graphblocks-documents",
    "graphblocks-policy",
    "graphblocks-rag",
    "graphblocks-stdlib",
    "graphblocks-usage",
}


def test_wheelhouse_gate_rejects_stale_artifacts_before_build(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "verify_wheelhouse",
        ROOT / "tools" / "verify_wheelhouse.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "graphblocks-9.9.9-py3-none-any.whl").write_bytes(b"stale")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("build must not start with stale wheels")
        ),
    )

    with pytest.raises(ValueError, match="wheelhouse must not contain existing wheel artifacts"):
        module.main(["--wheelhouse", str(wheelhouse)])


def test_graphblocks_artifact_owns_consolidated_namespace_and_cli() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "graphblocks"
    assert pyproject["project"]["scripts"] == {"graphblocks": "graphblocks.cli:main"}
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks"
    ]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "schemas": "graphblocks/schemas"
    }
    assert all(
        not dependency.startswith("graphblocks-")
        for dependency in pyproject["project"]["dependencies"]
    )
    assert pyproject["project"]["optional-dependencies"]["runtime"] == [
        "graphblocks-runtime>=0.1,<0.2"
    ]


def test_graphblocks_runtime_artifact_delegates_to_workspace_binding() -> None:
    pyproject = tomllib.loads(
        (ROOT / "packages" / "graphblocks-runtime" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert pyproject["project"]["name"] == "graphblocks-runtime"
    assert pyproject["project"]["dependencies"] == []
    assert pyproject["build-system"]["build-backend"] == "maturin"
    assert pyproject["tool"]["maturin"] == {
        "manifest-path": "../../crates/graphblocks-python/Cargo.toml",
        "module-name": "graphblocks_runtime._native",
        "python-source": "src",
        "features": ["extension-module"],
    }
    package_root = (
        ROOT / "packages" / "graphblocks-runtime" / "src" / "graphblocks_runtime"
    )
    assert (package_root / "__init__.py").is_file()
    assert (package_root / "py.typed").is_file()


def test_graphblocks_testing_is_the_only_additional_pure_python_artifact() -> None:
    pyproject = tomllib.loads(
        (ROOT / "packages" / "graphblocks-testing" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert pyproject["project"]["name"] == "graphblocks-testing"
    dependencies = pyproject["project"]["dependencies"]
    assert len(dependencies) == 1
    dependency = Requirement(dependencies[0])
    assert dependency.name == "graphblocks"
    assert Version(pyproject["project"]["version"]) in dependency.specifier
    assert pyproject["project"]["scripts"] == {
        "graphblocks-tck": "graphblocks_testing:main"
    }
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_testing"
    ]


def test_first_stable_distribution_versions_remain_in_lockstep() -> None:
    core = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testing = tomllib.loads(
        (ROOT / "packages" / "graphblocks-testing" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    runtime = tomllib.loads(
        (ROOT / "packages" / "graphblocks-runtime" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    stable_version = core["project"]["version"]
    assert stable_version == testing["project"]["version"] == graphblocks.__version__
    parsed_version = Version(stable_version)
    assert parsed_version.release == (1, 0, 0)
    assert parsed_version.pre is None or (
        parsed_version.pre[0] == "rc" and parsed_version.pre[1] >= 1
    )
    assert not parsed_version.is_devrelease
    assert not parsed_version.is_postrelease
    assert parsed_version.local is None
    assert runtime["project"]["version"] == "0.1.0"
    dependency = Requirement(testing["project"]["dependencies"][0])
    assert dependency.name == "graphblocks"
    assert Version(stable_version) in dependency.specifier


def test_load_package_catalog_rejects_invalid_artifact_and_component_shapes(
    tmp_path,
) -> None:
    cases = (
        ("[]\n", "package catalog must be a mapping"),
        (
            "catalogVersion: true\nspecVersion: '1.0'\nartifacts: []\ncomponents: []\n",
            "package catalog catalogVersion must be a positive integer",
        ),
        (
            "catalogVersion: 1\nspecVersion: ' '\nartifacts: []\ncomponents: []\n",
            "package catalog specVersion must be a non-empty string",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts: {}\ncomponents: []\n",
            "package catalog artifacts must be a list",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts:\n- graphblocks\ncomponents: []\n",
            "package catalog artifact entries must be mappings",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts:\n- kind: pure_python\ncomponents: []\n",
            "package catalog artifact distribution must be a non-empty string",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts: []\ncomponents: {}\n",
            "package catalog components must be a list",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts: []\ncomponents:\n- graphblocks-core\n",
            "package catalog component entries must be mappings",
        ),
        (
            "catalogVersion: 1\nspecVersion: '1.0'\nartifacts: []\ncomponents:\n- artifact: graphblocks\n",
            "package catalog component name must be a non-empty string",
        ),
    )

    for index, (document, message) in enumerate(cases):
        catalog_path = tmp_path / f"package-catalog-{index}.yaml"
        catalog_path.write_text(document, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_package_catalog(catalog_path)


def test_catalog_declares_three_python_artifacts_and_operator_delivery_artifact() -> None:
    catalog = load_package_catalog()
    artifacts = {
        artifact["distribution"]: artifact for artifact in catalog["artifacts"]
    }

    assert catalog["catalogVersion"] == 5
    assert "defaultMetaPackage" not in catalog
    assert "packages" not in catalog
    assert artifacts == {
        "graphblocks": {
            "distribution": "graphblocks",
            "import": "graphblocks",
            "kind": "pure_python",
            "manifest": "pyproject.toml",
            "versionConstraint": "~=0.1",
            "dependsOn": [],
        },
        "graphblocks-runtime": {
            "distribution": "graphblocks-runtime",
            "import": "graphblocks_runtime",
            "kind": "native_wheel",
            "manifest": "packages/graphblocks-runtime/pyproject.toml",
            "versionConstraint": "~=0.1",
            "dependsOn": [],
        },
        "graphblocks-testing": {
            "distribution": "graphblocks-testing",
            "import": "graphblocks_testing",
            "kind": "pure_python",
            "manifest": "packages/graphblocks-testing/pyproject.toml",
            "versionConstraint": "~=0.1",
            "dependsOn": ["graphblocks"],
        },
        "graphblocks-operator": {
            "distribution": "graphblocks-operator",
            "import": None,
            "kind": "oci_image_and_helm",
            "manifest": "packages/graphblocks-operator/Chart.yaml",
            "versionConstraint": "~=0.1",
            "dependsOn": [],
        },
    }


def test_catalog_preserves_logical_component_metadata_and_dependency_graph() -> None:
    catalog = load_package_catalog()
    components = {
        component["name"]: component for component in catalog["components"]
    }

    assert set(components) == EXPECTED_COMPONENTS
    required_fields = {
        "name",
        "artifact",
        "import",
        "layer",
        "default",
        "kind",
        "dependsOn",
        "responsibility",
        "implementationPhase",
        "stability",
    }
    for name, component in components.items():
        assert required_fields <= component.keys(), name
        assert all(dependency in components for dependency in component["dependsOn"]), name

    assert components["graphblocks-documents"]["forbiddenDependencies"] == [
        "parser SDKs",
        "OCR engines",
    ]
    assert components["graphblocks-agents"]["dependsOn"] == [
        "graphblocks-core",
        "graphblocks-conversation",
        "graphblocks-policy",
    ]
    assert components["graphblocks-operator"]["kind"] == "oci_image_and_helm"


def test_components_map_to_artifacts_and_consolidated_imports() -> None:
    components = {
        component["name"]: component for component in load_package_catalog()["components"]
    }

    for name, component in components.items():
        if name == "graphblocks-runtime":
            assert component["artifact"] == "graphblocks-runtime"
        elif name == "graphblocks-testing":
            assert component["artifact"] == "graphblocks-testing"
        elif name == "graphblocks-operator":
            assert component["artifact"] == "graphblocks-operator"
            assert component["import"] is None
        else:
            assert component["artifact"] == "graphblocks"

        if name in INTEGRATION_COMPONENTS:
            suffix = name.removeprefix("graphblocks-").replace("-", "_")
            assert component["import"] == f"graphblocks.integrations.{suffix}"
        elif name in DIRECT_IMPORTS:
            assert component["import"] == DIRECT_IMPORTS[name]

    assert set(components) == INTEGRATION_COMPONENTS | set(DIRECT_IMPORTS) | {
        "graphblocks-operator"
    }


def test_component_import_mappings_resolve_to_owned_artifact_sources() -> None:
    for component in load_package_catalog()["components"]:
        import_package = component["import"]
        if import_package is None or import_package in {"graphblocks", "graphblocks_testing"}:
            continue
        if import_package == "graphblocks_runtime":
            import_path = ROOT / "packages" / "graphblocks-runtime" / "src" / import_package
        else:
            import_path = ROOT / "src" / Path(*import_package.split("."))
        assert import_path.with_suffix(".py").is_file() or (
            import_path / "__init__.py"
        ).is_file(), component["name"]


def test_default_selection_and_release_conformance_reference_components() -> None:
    catalog = load_package_catalog()
    components = {
        component["name"]: component for component in catalog["components"]
    }
    selection = catalog["defaultSelection"]

    assert selection["artifacts"] == ["graphblocks"]
    assert set(selection["components"]) == DEFAULT_COMPONENTS
    assert {
        name for name, component in components.items() if component["default"]
    } == DEFAULT_COMPONENTS
    assert "model_provider" in selection["excludedCategories"]
    assert "voice" in selection["excludedCategories"]

    for train in catalog["releaseTrains"].values():
        assert all(
            component in components for component in train.get("components", [])
        )
        assert all(
            isinstance(check, str) and check
            for check in train.get("compatibilityBy", [])
        )
    for extension_group in catalog["extensionComponents"].values():
        assert all(component in components for component in extension_group)


def test_package_rows_project_component_and_artifact_identities() -> None:
    rows = {
        row["component"]: row for row in package_rows(load_package_catalog())
    }

    assert len(rows) == len(EXPECTED_COMPONENTS)
    assert rows["graphblocks-kafka"] == {
        "component": "graphblocks-kafka",
        "artifact": "graphblocks",
        "distribution": "graphblocks-kafka",
        "import": "graphblocks.integrations.kafka",
        "default": False,
        "layer": "durable_stream_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
    assert rows["graphblocks-operator"]["artifact"] == "graphblocks-operator"
    assert rows["graphblocks-operator"]["import"] is None


def test_package_lock_resolves_default_component_and_artifact_selection() -> None:
    lock = build_package_lock(load_package_catalog())
    entries = {entry.distribution: entry for entry in lock.entries}

    assert set(entries) == DEFAULT_COMPONENTS
    assert lock.artifacts == ("graphblocks",)
    assert entries["graphblocks-core"].artifact == "graphblocks"
    assert entries["graphblocks-core"].import_package == "graphblocks"
    assert all(entry.version_constraint == "~=0.1" for entry in lock.entries)
    assert not (
        {
            "graphblocks-openai",
            "graphblocks-pdf",
            "graphblocks-server",
            "graphblocks-voice",
        }
        & set(entries)
    )


def test_package_lock_selects_component_closure_and_required_artifacts() -> None:
    agents_lock = build_package_lock(
        load_package_catalog(),
        requested=("graphblocks-agents",),
        include_default=False,
    )
    testing_lock = build_package_lock(
        load_package_catalog(),
        requested=("graphblocks-testing",),
        include_default=False,
    )

    assert {entry.distribution for entry in agents_lock.entries} == {
        "graphblocks-agents",
        "graphblocks-conversation",
        "graphblocks-core",
        "graphblocks-policy",
    }
    assert agents_lock.artifacts == ("graphblocks",)
    assert {entry.distribution for entry in testing_lock.entries} == {
        "graphblocks-core",
        "graphblocks-testing",
    }
    assert set(testing_lock.artifacts) == {"graphblocks", "graphblocks-testing"}


def test_package_lock_can_select_an_artifact_without_activating_all_components() -> None:
    lock = build_package_lock(
        load_package_catalog(),
        requested=("graphblocks",),
        include_default=False,
    )

    assert lock.artifacts == ("graphblocks",)
    assert lock.entries == ()


def test_package_lock_payload_and_digest_are_canonical() -> None:
    catalog = load_package_catalog()
    left = build_package_lock(
        catalog,
        requested=("graphblocks-mcp", "graphblocks-openapi"),
        include_default=False,
    )
    right = build_package_lock(
        catalog,
        requested=("graphblocks-openapi", "graphblocks-mcp"),
        include_default=False,
    )

    assert left.lock_payload()["artifacts"] == ["graphblocks"]
    assert [entry["component"] for entry in left.lock_payload()["components"]] == [
        "graphblocks-core",
        "graphblocks-mcp",
        "graphblocks-openapi",
    ]
    assert left.lock_payload()["components"][1] == {
        "artifact": "graphblocks",
        "component": "graphblocks-mcp",
        "default": False,
        "dependencies": ["graphblocks-core"],
        "forbiddenDependencies": [],
        "import": "graphblocks.integrations.mcp",
        "kind": "pure_python",
        "layer": "integration",
        "stability": "integration",
        "versionConstraint": "~=0.1",
    }
    assert left.lock_payload()["requested"] == [
        "graphblocks-mcp",
        "graphblocks-openapi",
    ]
    assert left.content_digest().startswith("sha256:")
    assert left.content_digest() == right.content_digest()


def test_package_lock_canonicalizes_all_distribution_references() -> None:
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": ["Feature...Wheel"],
            "components": ["Feature_Component"],
            "excludedCategories": [],
        },
        "artifacts": [
            {"distribution": "Base.Wheel", "dependsOn": []},
            {
                "distribution": "Feature_Wheel",
                "dependsOn": ["BASE---WHEEL"],
                "versionConstraint": "~=1.0",
            },
        ],
        "components": [
            {
                "name": "Base.Component",
                "artifact": "BASE_WHEEL",
                "default": False,
                "dependsOn": [],
            },
            {
                "name": "Feature_Component",
                "artifact": "FEATURE...WHEEL",
                "default": True,
                "dependsOn": ["Base.Component"],
                "forbiddenDependencies": ["Blocked_Component"],
            },
        ],
    }

    lock = build_package_lock(
        catalog,
        requested=("Feature_Component", "FEATURE_WHEEL"),
    )
    entries = {entry.distribution: entry for entry in lock.entries}

    assert lock.requested == ("Feature_Component", "feature-wheel")
    assert lock.artifacts == ("base-wheel", "feature-wheel")
    assert set(entries) == {"Base.Component", "Feature_Component"}
    assert entries["Feature_Component"].artifact == "feature-wheel"
    assert entries["Feature_Component"].dependencies == ("Base.Component",)
    assert entries["Feature_Component"].forbidden_dependencies == (
        "Blocked_Component",
    )
    assert lock.entry("Feature_Component") == entries["Feature_Component"]
    assert lock.entry("feature-component") is None


def test_package_lock_prefers_an_exact_component_over_an_artifact_alias() -> None:
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "artifacts": [
            {"distribution": "foo-bar", "dependsOn": []},
            {"distribution": "other", "dependsOn": []},
        ],
        "components": [
            {
                "name": "Foo_Bar",
                "artifact": "other",
                "default": False,
                "dependsOn": [],
            }
        ],
    }

    lock = build_package_lock(
        catalog,
        requested=("Foo_Bar",),
        include_default=False,
    )

    assert lock.requested == ("Foo_Bar",)
    assert lock.artifacts == ("other",)
    assert [entry.component for entry in lock.entries] == ["Foo_Bar"]


def test_package_lock_rejects_unknown_selection_and_component_artifact() -> None:
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": [],
            "components": [],
            "excludedCategories": [],
        },
        "artifacts": [
            {
                "distribution": "graphblocks",
                "kind": "pure_python",
                "manifest": "pyproject.toml",
                "dependsOn": [],
            }
        ],
        "components": [
            {
                "name": "graphblocks-core",
                "artifact": "missing-artifact",
                "default": False,
                "dependsOn": [],
            }
        ],
    }

    with pytest.raises(ValueError, match="unknown package selection"):
        build_package_lock(catalog, requested=("missing-component",), include_default=False)
    with pytest.raises(ValueError, match="maps to unknown artifact"):
        build_package_lock(
            catalog,
            requested=("graphblocks-core",),
            include_default=False,
        )

    artifact_cycle_catalog = {
        **catalog,
        "artifacts": [
            {"distribution": "graphblocks", "dependsOn": ["graphblocks-runtime"]},
            {"distribution": "graphblocks-runtime", "dependsOn": ["graphblocks"]},
        ],
        "components": [],
    }
    with pytest.raises(ValueError, match="artifact dependency cycle"):
        build_package_lock(
            artifact_cycle_catalog,
            requested=("graphblocks",),
            include_default=False,
        )


def test_package_lock_rejects_forbidden_and_excluded_component_closures() -> None:
    forbidden_catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": [],
            "components": [],
            "excludedCategories": [],
        },
        "artifacts": [
            {"distribution": "graphblocks", "dependsOn": []},
        ],
        "components": [
            {
                "name": "graphblocks-documents",
                "artifact": "graphblocks",
                "default": False,
                "dependsOn": ["graphblocks-pdf"],
                "forbiddenDependencies": ["pypdf"],
            },
            {
                "name": "graphblocks-pdf",
                "artifact": "graphblocks",
                "default": False,
                "dependsOn": ["pypdf"],
            },
            {
                "name": "pypdf",
                "artifact": "graphblocks",
                "default": False,
                "dependsOn": [],
            },
        ],
    }
    excluded_catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": ["graphblocks"],
            "components": ["graphblocks-openai"],
            "excludedCategories": ["model_provider"],
        },
        "artifacts": [
            {"distribution": "graphblocks", "dependsOn": []},
        ],
        "components": [
            {
                "name": "graphblocks-openai",
                "artifact": "graphblocks",
                "default": True,
                "dependsOn": [],
                "categories": ["model_provider"],
            },
        ],
    }

    with pytest.raises(ValueError, match="forbidden package dependency"):
        build_package_lock(
            forbidden_catalog,
            requested=("graphblocks-documents",),
            include_default=False,
        )
    with pytest.raises(ValueError, match="excluded category"):
        build_package_lock(excluded_catalog)


def test_package_lock_records_validate_component_artifact_and_uniqueness() -> None:
    entry = PackageLockEntry(
        distribution="graphblocks-core",
        artifact="graphblocks",
        version_constraint="~=0.1",
        import_package="graphblocks",
        default=True,
        layer="schema_authoring",
        kind="pure_python",
        stability="foundation",
        dependencies=["graphblocks-schema"],  # type: ignore[arg-type]
        forbidden_dependencies=["requests"],  # type: ignore[arg-type]
    )

    assert entry.dependencies == ("graphblocks-schema",)
    assert entry.forbidden_dependencies == ("requests",)
    assert entry.component == "graphblocks-core"
    with pytest.raises(ValueError, match="package lock entry distribution must not be empty"):
        PackageLockEntry(" ", "graphblocks", None, None, True, None, None, None)
    with pytest.raises(ValueError, match="package lock entry artifact must not be empty"):
        PackageLockEntry("graphblocks-core", " ", None, None, True, None, None, None)
    with pytest.raises(ValueError, match="package lock entry default must be a boolean"):
        PackageLockEntry(  # type: ignore[arg-type]
            "graphblocks-core",
            "graphblocks",
            None,
            None,
            "yes",
            None,
            None,
            None,
        )

    lock = PackageLock(
        1,
        "1.0",
        requested=["graphblocks-core"],  # type: ignore[arg-type]
        entries=[entry],  # type: ignore[arg-type]
        artifacts=["graphblocks"],  # type: ignore[arg-type]
    )

    assert lock.requested == ("graphblocks-core",)
    assert lock.entries == (entry,)
    assert lock.artifacts == ("graphblocks",)
    with pytest.raises(ValueError, match="package lock catalog_version must be positive"):
        PackageLock(
            0,
            "1.0",
            requested=("graphblocks-core",),
            entries=(entry,),
        )
    with pytest.raises(ValueError, match="package lock entries must have unique distributions"):
        PackageLock(
            1,
            "1.0",
            requested=("graphblocks-core",),
            entries=(entry, entry),
        )
    with pytest.raises(ValueError, match="package lock artifacts must be unique"):
        PackageLock(
            1,
            "1.0",
            requested=("graphblocks-core",),
            entries=(entry,),
            artifacts=("graphblocks", "graphblocks"),
        )


@pytest.mark.parametrize("alias", ("graphblocks_core", "graphblocks.core", "graphblocks---core"))
def test_package_lock_preserves_exact_component_identity(alias: str) -> None:
    entry = PackageLockEntry(
        "graphblocks-core",
        "graphblocks",
        None,
        None,
        True,
        None,
        None,
        None,
    )
    aliased_component_entry = PackageLockEntry(
        alias,
        "graphblocks",
        None,
        None,
        False,
        None,
        None,
        None,
    )

    lock = PackageLock(
        1,
        "1.0",
        requested=(),
        entries=(entry, aliased_component_entry),
    )

    assert [item.component for item in lock.entries] == ["graphblocks-core", alias]
    with pytest.raises(ValueError, match="package lock artifacts must be unique"):
        PackageLock(
            1,
            "1.0",
            requested=(),
            entries=(entry,),
            artifacts=("graphblocks-core", alias),
        )


def test_package_catalog_doctor_accepts_builtin_catalog_and_artifact_manifests() -> None:
    assert doctor_package_catalog(load_package_catalog()).diagnostics == ()

    diagnostics = doctor_package_catalog(load_package_catalog(), root=ROOT)

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


@pytest.mark.parametrize("duplicate", ("same_wheel", "same.wheel", "same---wheel"))
def test_package_catalog_doctor_rejects_canonical_distribution_duplicates(
    duplicate: str,
) -> None:
    diagnostics = doctor_package_catalog(
        {
            "artifacts": [
                {"distribution": "same-wheel", "dependsOn": []},
                {"distribution": duplicate, "dependsOn": []},
            ],
            "components": [],
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == [
        "PackageArtifactDuplicateDistribution"
    ]


def test_package_catalog_doctor_resolves_all_canonical_distribution_references() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultSelection": {
                "artifacts": ["Feature...Wheel"],
                "components": ["Feature_Component"],
                "excludedCategories": [],
            },
            "artifacts": [
                {"distribution": "Base.Wheel", "dependsOn": []},
                {
                    "distribution": "Feature_Wheel",
                    "dependsOn": ["BASE---WHEEL"],
                },
            ],
            "components": [
                {
                    "name": "Base.Component",
                    "artifact": "BASE_WHEEL",
                    "default": False,
                    "dependsOn": [],
                },
                {
                    "name": "Feature_Component",
                    "artifact": "FEATURE...WHEEL",
                    "default": True,
                    "dependsOn": ["Base.Component"],
                },
            ],
            "releaseTrains": {
                "extensions": {"components": ["Feature_Component"]},
            },
            "extensionComponents": {
                "features": ["Feature_Component"],
            },
        }
    )

    assert diagnostics.diagnostics == ()


def test_package_catalog_doctor_reports_component_mapping_and_conformance_errors() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultSelection": {
                "artifacts": ["missing-artifact"],
                "components": ["missing-component"],
                "excludedCategories": [],
            },
            "artifacts": [
                {
                    "distribution": "graphblocks",
                    "dependsOn": ["missing-artifact"],
                }
            ],
            "components": [
                {
                    "name": "graphblocks-core",
                    "artifact": "missing-artifact",
                    "import": " ",
                    "default": True,
                    "dependsOn": ["missing-component"],
                }
            ],
            "releaseTrains": {
                "extensions": {
                    "components": ["missing-component"],
                    "compatibilityBy": [""],
                }
            },
            "extensionComponents": {"operations": ["missing-component"]},
        }
    )
    codes = {diagnostic.code for diagnostic in diagnostics.diagnostics}

    assert {
        "PackageArtifactDependencyMissing",
        "PackageComponentArtifactUnknown",
        "PackageComponentImportInvalid",
        "PackageComponentDependencyMissing",
        "PackageDefaultArtifactMissing",
        "PackageDefaultComponentMissing",
        "PackageDefaultComponentMismatch",
        "PackageReleaseTrainComponentMissing",
        "PackageReleaseTrainConformanceInvalid",
        "PackageExtensionComponentMissing",
    } <= codes


def test_package_catalog_doctor_reports_forbidden_closure_and_component_cycle() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultSelection": {
                "artifacts": [],
                "components": [],
                "excludedCategories": [],
            },
            "artifacts": [{"distribution": "graphblocks", "dependsOn": []}],
            "components": [
                {
                    "name": "graphblocks-documents",
                    "artifact": "graphblocks",
                    "default": False,
                    "dependsOn": ["graphblocks-pdf"],
                    "forbiddenDependencies": ["pypdf"],
                },
                {
                    "name": "graphblocks-pdf",
                    "artifact": "graphblocks",
                    "default": False,
                    "dependsOn": ["pypdf"],
                },
                {
                    "name": "pypdf",
                    "artifact": "graphblocks",
                    "default": False,
                    "dependsOn": ["graphblocks-documents"],
                },
            ],
        }
    )

    assert {item.code for item in diagnostics.diagnostics} == {
        "PackageForbiddenDependencySelected",
        "PackageComponentDependencyCycle",
    }


def test_package_catalog_doctor_reports_default_excluded_component_category() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultSelection": {
                "artifacts": ["graphblocks"],
                "components": ["graphblocks-openai"],
                "excludedCategories": ["model_provider"],
            },
            "artifacts": [{"distribution": "graphblocks", "dependsOn": []}],
            "components": [
                {
                    "name": "graphblocks-openai",
                    "artifact": "graphblocks",
                    "default": True,
                    "dependsOn": [],
                    "categories": ["model_provider"],
                }
            ],
        }
    )

    assert [(item.code, item.message) for item in diagnostics.diagnostics] == [
        (
            "PackageDefaultIncludesExcludedCategory",
            (
                "default component closure includes excluded category "
                "'model_provider' from component 'graphblocks-openai'"
            ),
        )
    ]


def test_package_catalog_doctor_reports_artifact_manifest_dependency_drift(
    tmp_path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "graphblocks"
version = "0.1.0"
dependencies = []
""".strip(),
        encoding="utf-8",
    )
    testing_manifest = tmp_path / "testing" / "pyproject.toml"
    testing_manifest.parent.mkdir()
    testing_manifest.write_text(
        """
[project]
name = "graphblocks-testing"
version = "0.1.0"
dependencies = []
""".strip(),
        encoding="utf-8",
    )
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": [],
            "components": [],
            "excludedCategories": [],
        },
        "artifacts": [
            {
                "distribution": "graphblocks",
                "kind": "pure_python",
                "manifest": "pyproject.toml",
                "dependsOn": [],
            },
            {
                "distribution": "graphblocks-testing",
                "kind": "pure_python",
                "manifest": "testing/pyproject.toml",
                "dependsOn": ["graphblocks"],
            },
        ],
        "components": [],
    }

    diagnostics = doctor_package_catalog(catalog, root=tmp_path)

    assert [(item.code, item.message) for item in diagnostics.diagnostics] == [
        (
            "PackageManifestDependencyMissing",
            (
                "artifact manifest for 'graphblocks-testing' is missing "
                "catalog dependency 'graphblocks'"
            ),
        )
    ]


@pytest.mark.parametrize(
    ("requirement", "expected_code"),
    [
        ("graphblocks>=1.0", "PackageManifestDependencyVersionUnsatisfied"),
        ("graphblocks=>0.1", "PackageManifestDependencyRequirementInvalid"),
    ],
)
def test_package_catalog_doctor_fails_closed_for_artifact_requirements(
    tmp_path,
    requirement,
    expected_code,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "graphblocks"
version = "0.1.0"
dependencies = []
""".strip(),
        encoding="utf-8",
    )
    testing_manifest = tmp_path / "testing" / "pyproject.toml"
    testing_manifest.parent.mkdir()
    testing_manifest.write_text(
        f"""
[project]
name = "graphblocks-testing"
version = "0.1.0"
dependencies = ["{requirement}"]
""".strip(),
        encoding="utf-8",
    )
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultSelection": {
            "artifacts": [],
            "components": [],
            "excludedCategories": [],
        },
        "artifacts": [
            {
                "distribution": "graphblocks",
                "kind": "pure_python",
                "manifest": "pyproject.toml",
                "dependsOn": [],
            },
            {
                "distribution": "graphblocks-testing",
                "kind": "pure_python",
                "manifest": "testing/pyproject.toml",
                "dependsOn": ["graphblocks"],
            },
        ],
        "components": [],
    }

    diagnostics = doctor_package_catalog(catalog, root=tmp_path)

    assert [item.code for item in diagnostics.diagnostics] == [expected_code]


def test_package_wheel_matrix_is_exactly_the_three_python_artifacts() -> None:
    matrix = build_wheel_matrix(ROOT, python_versions=("3.11", "3.12"))
    targets = {target.distribution: target for target in matrix.targets}

    assert matrix.ok
    assert matrix.diagnostics == ()
    assert set(targets) == {
        "graphblocks",
        "graphblocks-runtime",
        "graphblocks-testing",
    }
    assert targets["graphblocks"].target_contract() == {
        "distribution": "graphblocks",
        "manifest": "pyproject.toml",
        "backend": "hatchling.build",
        "kind": "pure_python",
        "source_layout": "src/graphblocks",
        "python_versions": ["3.11", "3.12"],
    }
    assert targets["graphblocks-runtime"].target_contract() == {
        "distribution": "graphblocks-runtime",
        "manifest": "packages/graphblocks-runtime/pyproject.toml",
        "backend": "maturin",
        "kind": "native_extension",
        "source_layout": "src",
        "python_versions": ["3.11", "3.12"],
    }
    assert targets["graphblocks-testing"].target_contract() == {
        "distribution": "graphblocks-testing",
        "manifest": "packages/graphblocks-testing/pyproject.toml",
        "backend": "hatchling.build",
        "kind": "pure_python",
        "source_layout": "src/graphblocks_testing",
        "python_versions": ["3.11", "3.12"],
    }
    assert matrix.matrix_contract()["target_count"] == 3
    assert matrix.content_digest().startswith("sha256:")


@pytest.mark.parametrize(
    ("requires_python", "python_versions", "expected_versions"),
    [
        (">=3.11,!=3.12.*", ("3.11", "3.12"), ("3.11",)),
        ("~=3.11.0", ("3.11", "3.12", "3.13"), ("3.11",)),
        ("===3.11", ("3.11", "3.12"), ("3.11",)),
    ],
)
def test_wheel_matrix_honors_pep440_python_constraints(
    tmp_path,
    requires_python: str,
    python_versions: tuple[str, ...],
    expected_versions: tuple[str, ...],
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        f"""
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "selected"
version = "0.1.0"
requires-python = "{requires_python}"

[tool.hatch.build.targets.wheel]
packages = ["src/selected"]
""".strip(),
        encoding="utf-8",
    )
    matrix = build_wheel_matrix(
        tmp_path,
        python_versions=python_versions,
        catalog={
            "artifacts": [
                {
                    "distribution": "selected",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )

    assert not matrix.ok
    assert [item.code for item in matrix.diagnostics] == ["WheelPythonVersionUnsupported"]
    assert matrix.targets[0].python_versions == expected_versions


def test_wheel_matrix_uses_only_catalog_declared_artifacts(tmp_path) -> None:
    selected = tmp_path / "selected" / "pyproject.toml"
    selected.parent.mkdir()
    selected.write_text(
        """
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "selected"
version = "0.1.0"
requires-python = ">=3.11"

[tool.hatch.build.targets.wheel]
packages = ["src/selected"]
""".strip(),
        encoding="utf-8",
    )
    legacy = tmp_path / "packages" / "legacy" / "pyproject.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[project]\nname = 'legacy'\n", encoding="utf-8")
    catalog = {
        "artifacts": [
            {
                "distribution": "selected",
                "kind": "pure_python",
                "manifest": "selected/pyproject.toml",
            },
            {
                "distribution": "operator",
                "kind": "oci_image_and_helm",
                "manifest": "operator/Chart.yaml",
            },
        ]
    }

    matrix = build_wheel_matrix(tmp_path, catalog=catalog)

    assert matrix.ok
    assert [target.distribution for target in matrix.targets] == ["selected"]


def test_package_wheel_matrix_reports_missing_build_target(tmp_path) -> None:
    pyproject = tmp_path / "packages" / "broken-wheel" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text(
        """
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "broken-wheel"
version = "0.1.0"
requires-python = ">=3.11"
""".strip(),
        encoding="utf-8",
    )
    catalog = {
        "artifacts": [
            {
                "distribution": "broken-wheel",
                "kind": "pure_python",
                "manifest": "packages/broken-wheel/pyproject.toml",
            }
        ]
    }

    matrix = build_wheel_matrix(
        tmp_path,
        python_versions=("3.11", "3.12"),
        catalog=catalog,
    )

    assert not matrix.ok
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelBuildTargetMissing", "$.packages/broken-wheel/pyproject.toml.tool")
    ]


def test_package_wheel_matrix_reports_artifact_manifest_identity_mismatch(
    tmp_path,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        """
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "wrong-name"
version = "0.1.0"
requires-python = ">=3.11"

[tool.hatch.build.targets.wheel]
packages = ["src/wrong_name"]
""".strip(),
        encoding="utf-8",
    )

    matrix = build_wheel_matrix(
        tmp_path,
        catalog={
            "artifacts": [
                {
                    "distribution": "expected-name",
                    "kind": "pure_python",
                    "manifest": "pyproject.toml",
                }
            ]
        },
    )

    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelDistributionMismatch", "$.pyproject.toml.project.name")
    ]


def test_wheel_matrix_records_validate_identity_and_collection_types() -> None:
    target = WheelBuildTarget(
        distribution="graphblocks",
        manifest="pyproject.toml",
        backend="hatchling.build",
        kind="pure_python",
        source_layout="src/graphblocks",
        python_versions=["3.11", "3.12"],  # type: ignore[arg-type]
    )

    assert target.python_versions == ("3.11", "3.12")
    with pytest.raises(ValueError, match="wheel build target distribution must not be empty"):
        WheelBuildTarget(
            " ",
            "pyproject.toml",
            "hatchling.build",
            "pure_python",
            "src/graphblocks",
            ("3.11",),
        )
    with pytest.raises(ValueError, match="invalid wheel build target kind"):
        WheelBuildTarget(
            "graphblocks",
            "pyproject.toml",
            "hatchling.build",
            "binary",  # type: ignore[arg-type]
            "src/graphblocks",
            ("3.11",),
        )
    with pytest.raises(
        ValueError,
        match="wheel build target python_versions item must not be empty",
    ):
        WheelBuildTarget(
            "graphblocks",
            "pyproject.toml",
            "hatchling.build",
            "pure_python",
            "src/graphblocks",
            (" ",),
        )

    matrix = WheelMatrix(targets=[target])  # type: ignore[arg-type]

    assert matrix.targets == (target,)
    with pytest.raises(ValueError, match="wheel matrix targets must have unique distributions"):
        WheelMatrix(targets=(target, target))
    with pytest.raises(ValueError, match="wheel matrix diagnostics must be Diagnostic"):
        WheelMatrix(
            targets=(target,),
            diagnostics=(object(),),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("duplicate", ("same_wheel", "same.wheel", "same---wheel"))
def test_wheel_matrix_rejects_canonical_distribution_duplicates(duplicate: str) -> None:
    def target(distribution: str) -> WheelBuildTarget:
        return WheelBuildTarget(
            distribution=distribution,
            manifest=f"packages/{distribution}/pyproject.toml",
            backend="hatchling.build",
            kind="pure_python",
            source_layout=f"src/{distribution}",
            python_versions=("3.11", "3.12"),
        )

    with pytest.raises(ValueError, match="wheel matrix targets must have unique distributions"):
        WheelMatrix(targets=(target("same-wheel"), target(duplicate)))


def test_package_manifest_audit_accepts_repo_manifest_licenses() -> None:
    diagnostics = audit_package_manifests(ROOT)

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


def test_package_manifest_audit_policy_validates_and_normalizes_collections() -> None:
    policy = PackageManifestAuditPolicy(
        allowed_licenses=("MIT", "Apache-2.0", "MIT"),
        blocked_dependencies=("vulnerable_sdk", "Vulnerable-SDK"),
    )

    assert policy.allowed_licenses == ("Apache-2.0", "MIT")
    assert policy.blocked_dependencies == ("vulnerable-sdk",)
    with pytest.raises(ValueError, match="allowed_licenses item must not be empty"):
        PackageManifestAuditPolicy(allowed_licenses=(" ",))
    with pytest.raises(ValueError, match="blocked_dependencies must be a collection"):
        PackageManifestAuditPolicy(blocked_dependencies="requests")  # type: ignore[arg-type]


def test_package_manifest_audit_reports_denied_license_and_blocked_dependency(
    tmp_path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "unsafe-python"
version = "0.1.0"
license = "Proprietary"
dependencies = ["vulnerable-sdk>=1"]
""".strip(),
        encoding="utf-8",
    )

    diagnostics = audit_package_manifests(
        tmp_path,
        policy=PackageManifestAuditPolicy(
            allowed_licenses=("Apache-2.0",),
            blocked_dependencies=("vulnerable-sdk",),
        ),
    )

    assert [item.code for item in diagnostics.diagnostics] == [
        "PackageLicenseDenied",
        "PackageBlockedDependency",
    ]


def test_package_manifest_audit_checks_build_and_dependency_group_requirements(
    tmp_path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["unsafe-build>=1"]
build-backend = "unsafe.build"

[project]
name = "unsafe-python"
version = "0.1.0"
license = "Apache-2.0"
dependencies = []

[dependency-groups]
dev = ["unsafe-dev>=1"]
""".strip(),
        encoding="utf-8",
    )

    diagnostics = audit_package_manifests(
        tmp_path,
        policy=PackageManifestAuditPolicy(
            blocked_dependencies=("unsafe-build", "unsafe-dev"),
        ),
    )

    assert [(item.code, item.path) for item in diagnostics.diagnostics] == [
        (
            "PackageBlockedDependency",
            "$.pyproject.toml.build-system.requires[0]",
        ),
        (
            "PackageBlockedDependency",
            "$.pyproject.toml.dependency-groups.dev[0]",
        ),
    ]
