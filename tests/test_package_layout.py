from __future__ import annotations

from pathlib import Path
import tomllib

from graphblocks.packages import build_package_lock, doctor_package_catalog, load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def test_graphblocks_python_crate_is_workspace_member() -> None:
    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))

    assert "crates/graphblocks-python" in workspace["workspace"]["members"]

    crate = tomllib.loads(
        (ROOT / "crates" / "graphblocks-python" / "Cargo.toml").read_text(encoding="utf-8")
    )
    assert crate["package"]["name"] == "graphblocks-python"
    assert crate["lib"]["name"] == "graphblocks_python"
    assert "cdylib" in crate["lib"]["crate-type"]
    assert crate["dependencies"]["graphblocks-runtime-core"]["path"] == "../graphblocks-runtime-core"
    assert crate["dependencies"]["graphblocks-protocol"]["path"] == "../graphblocks-protocol"
    assert "pyo3" in crate["dependencies"]


def test_schema_and_types_crates_are_workspace_members() -> None:
    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))

    assert "crates/graphblocks-schema" in workspace["workspace"]["members"]
    assert "crates/graphblocks-types" in workspace["workspace"]["members"]

    schema_crate = tomllib.loads(
        (ROOT / "crates" / "graphblocks-schema" / "Cargo.toml").read_text(encoding="utf-8")
    )
    assert schema_crate["package"]["name"] == "graphblocks-schema"

    types_crate = tomllib.loads(
        (ROOT / "crates" / "graphblocks-types" / "Cargo.toml").read_text(encoding="utf-8")
    )
    assert types_crate["package"]["name"] == "graphblocks-types"
    assert types_crate["dependencies"]["graphblocks-schema"]["path"] == "../graphblocks-schema"


def test_graphblocks_runtime_package_delegates_to_workspace_binding() -> None:
    pyproject = tomllib.loads(
        (ROOT / "packages" / "graphblocks-runtime" / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["build-system"]["build-backend"] == "maturin"
    assert pyproject["project"]["name"] == "graphblocks-runtime"
    assert pyproject["tool"]["maturin"]["manifest-path"] == "../../crates/graphblocks-python/Cargo.toml"
    assert pyproject["tool"]["maturin"]["module-name"] == "graphblocks_runtime._native"
    assert pyproject["tool"]["maturin"]["python-source"] == "src"
    assert pyproject["tool"]["maturin"]["features"] == ["extension-module"]
    package_root = ROOT / "packages" / "graphblocks-runtime" / "src" / "graphblocks_runtime"
    assert (package_root / "__init__.py").exists()
    assert (package_root / "py.typed").exists()
    wrapper = (package_root / "__init__.py").read_text(encoding="utf-8")
    assert "admit_exhaustion_work_json" in wrapper
    assert "evaluate_output_gate_json" in wrapper
    assert "validate_worker_advertisement_json" in wrapper
    assert "validate_remote_payload_json" in wrapper


def test_graphblocks_core_distribution_owns_graphblocks_import_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["name"] == "graphblocks-core"
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/graphblocks"]
    assert "scripts" not in pyproject["project"]


def test_graphblocks_metapackage_is_dependency_only() -> None:
    package_root = ROOT / "packages" / "graphblocks"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks"
    assert pyproject["project"]["dependencies"] == [
        "graphblocks-core~=1.0",
        "graphblocks-runtime~=1.0",
        "graphblocks-stdlib~=1.0",
        "graphblocks-documents~=1.0",
        "graphblocks-rag~=1.0",
        "graphblocks-conversation~=1.0",
        "graphblocks-policy~=1.0",
        "graphblocks-budget~=1.0",
        "graphblocks-usage~=1.0",
        "graphblocks-cli~=1.0",
    ]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"] == [
        "METAPACKAGE.md"
    ]
    assert (package_root / "METAPACKAGE.md").exists()
    assert not (package_root / "src" / "graphblocks").exists()


def test_tool_adapter_packages_are_cataloged_as_optional_integrations() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-mcp"] == {
        "distribution": "graphblocks-mcp",
        "import": "graphblocks_mcp",
        "default": False,
        "layer": "integration",
        "kind": "pure_python",
        "implementationPhase": 2,
        "stability": "integration",
    }
    assert rows["graphblocks-openapi"] == {
        "distribution": "graphblocks-openapi",
        "import": "graphblocks_openapi",
        "default": False,
        "layer": "integration",
        "kind": "pure_python",
        "implementationPhase": 2,
        "stability": "integration",
    }


def test_tool_adapter_packages_have_pure_python_layouts() -> None:
    for distribution, import_name in (
        ("graphblocks-mcp", "graphblocks_mcp"),
        ("graphblocks-openapi", "graphblocks_openapi"),
    ):
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert pyproject["project"]["dependencies"] == ["graphblocks-core~=1.0"]
        assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            f"src/{import_name}"
        ]
        assert (package_root / "src" / import_name / "__init__.py").exists()
        assert (package_root / "src" / import_name / "py.typed").exists()


def test_stdlib_package_has_pure_python_layout() -> None:
    package_root = ROOT / "packages" / "graphblocks-stdlib"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-stdlib"
    assert pyproject["project"]["dependencies"] == ["graphblocks-core~=1.0"]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_stdlib"
    ]
    assert (package_root / "src" / "graphblocks_stdlib" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_stdlib" / "py.typed").exists()


def test_documents_package_has_pure_python_layout_without_parser_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-documents"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-documents"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any("pdf" in dependency.lower() or "ocr" in dependency.lower() for dependency in dependencies)
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_documents"
    ]
    assert (package_root / "src" / "graphblocks_documents" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_documents" / "py.typed").exists()


def test_rag_package_has_pure_python_layout_without_vector_db_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-rag"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-rag"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        vector_client in dependency.lower()
        for dependency in dependencies
        for vector_client in ("qdrant", "pinecone", "weaviate", "opensearch")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_rag"
    ]
    assert (package_root / "src" / "graphblocks_rag" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_rag" / "py.typed").exists()


def test_conversation_package_has_pure_python_layout_without_server_or_db_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-conversation"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-conversation"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        forbidden in dependency.lower()
        for dependency in dependencies
        for forbidden in ("fastapi", "django", "sqlalchemy", "psycopg", "redis")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_conversation"
    ]
    assert (package_root / "src" / "graphblocks_conversation" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_conversation" / "py.typed").exists()


def test_budget_package_has_pure_python_layout_without_backend_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-budget"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-budget"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        backend in dependency.lower()
        for dependency in dependencies
        for backend in ("sqlalchemy", "psycopg", "asyncpg", "redis")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_budget"
    ]
    assert (package_root / "src" / "graphblocks_budget" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_budget" / "py.typed").exists()


def test_usage_package_has_pure_python_layout_without_backend_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-usage"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-usage"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        backend in dependency.lower()
        for dependency in dependencies
        for backend in ("sqlalchemy", "psycopg", "asyncpg", "redis")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_usage"
    ]
    assert (package_root / "src" / "graphblocks_usage" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_usage" / "py.typed").exists()


def test_policy_package_has_pure_python_layout_without_external_pdp_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-policy"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-policy"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any("opa" in dependency.lower() or "cedar" in dependency.lower() for dependency in dependencies)
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_policy"
    ]
    assert (package_root / "src" / "graphblocks_policy" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_policy" / "py.typed").exists()


def test_cli_package_has_python_entrypoint_layout_without_native_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-cli"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-cli"
    assert pyproject["project"]["dependencies"] == ["graphblocks-core~=1.0"]
    assert "maturin" not in pyproject
    assert pyproject["project"]["scripts"] == {"graphblocks": "graphblocks_cli:main"}
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_cli"
    ]
    assert (package_root / "src" / "graphblocks_cli" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_cli" / "py.typed").exists()


def test_agents_package_has_pure_python_layout_without_provider_sdk_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-agents"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-agents"
    assert dependencies == [
        "graphblocks-core~=1.0",
        "graphblocks-conversation~=1.0",
        "graphblocks-policy~=1.0",
    ]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("openai", "anthropic", "boto3", "google")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_agents"
    ]
    assert (package_root / "src" / "graphblocks_agents" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_agents" / "py.typed").exists()


def test_evaluation_package_has_pure_python_layout_without_model_provider_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-evaluation"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-evaluation"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("openai", "anthropic", "boto3", "google")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_evaluation"
    ]
    assert (package_root / "src" / "graphblocks_evaluation" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_evaluation" / "py.typed").exists()


def test_testing_package_has_pure_python_layout_without_provider_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-testing"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-testing"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("openai", "anthropic", "boto3", "google")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_testing"
    ]
    assert (package_root / "src" / "graphblocks_testing" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_testing" / "py.typed").exists()


def test_client_package_has_pure_python_layout_without_server_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-client"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-client"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        framework in dependency.lower()
        for dependency in dependencies
        for framework in ("fastapi", "starlette", "django", "flask", "aiohttp")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_client"
    ]
    assert (package_root / "src" / "graphblocks_client" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_client" / "py.typed").exists()


def test_audit_package_has_pure_python_layout_without_backend_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-audit"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-audit"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        backend in dependency.lower()
        for dependency in dependencies
        for backend in ("sqlalchemy", "psycopg", "asyncpg", "redis", "kafka")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_audit"
    ]
    assert (package_root / "src" / "graphblocks_audit" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_audit" / "py.typed").exists()


def test_deployment_package_has_pure_python_layout_without_platform_sdk_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-deployment"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-deployment"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        platform in dependency.lower()
        for dependency in dependencies
        for platform in ("kubernetes", "terraform", "boto3", "google-cloud", "azure")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_deployment"
    ]
    assert (package_root / "src" / "graphblocks_deployment" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_deployment" / "py.typed").exists()


def test_policy_adapter_packages_have_pure_python_layouts_without_sdk_dependencies() -> None:
    for distribution, import_name in (
        ("graphblocks-policy-opa", "graphblocks_policy_opa"),
        ("graphblocks-policy-cedar", "graphblocks_policy_cedar"),
    ):
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == ["graphblocks-core~=1.0"]
        assert not any("opa" in dependency.lower() or "cedar" in dependency.lower() for dependency in dependencies)
        assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            f"src/{import_name}"
        ]
        assert (package_root / "src" / import_name / "__init__.py").exists()
        assert (package_root / "src" / import_name / "py.typed").exists()


def test_observability_projection_packages_have_pure_python_layouts_without_sdk_dependencies() -> None:
    cases = (
        ("graphblocks-telemetry", "graphblocks_telemetry", ["graphblocks-core~=1.0"]),
        ("graphblocks-otel", "graphblocks_otel", ["graphblocks-telemetry~=1.0"]),
        ("graphblocks-langfuse", "graphblocks_langfuse", ["graphblocks-telemetry~=1.0"]),
    )
    for distribution, import_name, expected_dependencies in cases:
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == expected_dependencies
        assert not any(
            "opentelemetry" in dependency.lower() or "langfuse" in dependency.lower()
            for dependency in dependencies
        )
        assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            f"src/{import_name}"
        ]
        assert (package_root / "src" / import_name / "__init__.py").exists()
        assert (package_root / "src" / import_name / "py.typed").exists()


def test_package_lock_resolves_default_metapackage_closure_without_optional_integrations() -> None:
    lock = build_package_lock(load_package_catalog(), requested=("graphblocks",))

    assert lock.catalog_version == 4
    assert lock.spec_version == "1.0"
    assert lock.requested == ("graphblocks",)
    assert [entry.distribution for entry in lock.entries] == [
        "graphblocks",
        "graphblocks-budget",
        "graphblocks-cli",
        "graphblocks-conversation",
        "graphblocks-core",
        "graphblocks-documents",
        "graphblocks-policy",
        "graphblocks-rag",
        "graphblocks-runtime",
        "graphblocks-stdlib",
        "graphblocks-usage",
    ]
    assert "graphblocks-mcp" not in {entry.distribution for entry in lock.entries}
    assert "model_provider" in lock.excluded_categories
    assert lock.entry("graphblocks-core").version_constraint == "~=1.0"
    assert lock.entry("graphblocks-openapi") is None


def test_package_lock_includes_requested_extension_and_transitive_dependencies() -> None:
    lock = build_package_lock(load_package_catalog(), requested=("graphblocks-agents",), include_default=False)

    assert [entry.distribution for entry in lock.entries] == [
        "graphblocks-agents",
        "graphblocks-conversation",
        "graphblocks-core",
        "graphblocks-policy",
    ]
    assert lock.entry("graphblocks-agents").default is False
    assert lock.entry("graphblocks-core").dependencies == ()
    assert lock.entry("graphblocks-conversation").dependencies == ("graphblocks-core",)


def test_package_catalog_doctor_accepts_builtin_catalog() -> None:
    diagnostics = doctor_package_catalog(load_package_catalog())

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


def test_package_catalog_doctor_reports_unknown_dependency_and_default_constraint() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultMetaPackage": {
                "distribution": "graphblocks",
                "dependencies": ["missing-default~=1.0"],
                "excludedCategories": [],
            },
            "packages": [
                {
                    "distribution": "graphblocks",
                    "default": True,
                    "dependsOn": ["missing-runtime"],
                }
            ],
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == [
        "PackageDefaultDependencyMissing",
        "PackageDependencyMissing",
    ]


def test_package_catalog_doctor_reports_dependency_cycles() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultMetaPackage": {"distribution": "graphblocks", "dependencies": [], "excludedCategories": []},
            "packages": [
                {"distribution": "graphblocks", "default": True, "dependsOn": []},
                {"distribution": "graphblocks-core", "default": True, "dependsOn": ["graphblocks-runtime"]},
                {"distribution": "graphblocks-runtime", "default": True, "dependsOn": ["graphblocks-core"]},
            ],
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["PackageDependencyCycle"]
