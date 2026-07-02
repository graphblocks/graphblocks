from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from graphblocks.packages import (
    PackageLock,
    PackageLockEntry,
    PackageManifestAuditPolicy,
    WheelBuildTarget,
    WheelMatrix,
    build_wheel_matrix,
    audit_package_manifests,
    build_package_lock,
    doctor_package_catalog,
    load_package_catalog,
    package_rows,
)


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
    assert "decide_agent_step_json" in wrapper
    assert "evaluate_declarative_output_policy_json" in wrapper
    assert "evaluate_output_gate_json" in wrapper
    assert "finalize_tool_call_json" in wrapper
    assert "validate_worker_advertisement_json" in wrapper
    assert "validate_worker_protocol_message_json" in wrapper
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


def test_model_provider_adapter_packages_are_cataloged_as_optional_integrations() -> None:
    catalog = load_package_catalog()
    rows = {row["distribution"]: row for row in package_rows(catalog)}
    manifests = {manifest["distribution"]: manifest for manifest in catalog["packages"]}

    assert rows["graphblocks-openai"] == {
        "distribution": "graphblocks-openai",
        "import": "graphblocks_openai",
        "default": False,
        "layer": "model_provider_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
    assert "OpenAI usage ledger record conversion" in manifests["graphblocks-openai"][
        "responsibility"
    ]
    assert rows["graphblocks-scripted"] == {
        "distribution": "graphblocks-scripted",
        "import": "graphblocks_scripted",
        "default": False,
        "layer": "model_provider_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }


def test_model_provider_adapter_packages_have_pure_python_layouts_without_sdk_dependencies() -> None:
    for distribution, import_name in (
        ("graphblocks-openai", "graphblocks_openai"),
        ("graphblocks-scripted", "graphblocks_scripted"),
    ):
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == ["graphblocks-core~=1.0"]
        assert not any(
            provider in dependency.lower()
            for dependency in dependencies
            for provider in ("openai", "httpx", "requests", "aiohttp", "anthropic", "google")
        )
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


def test_document_parser_packages_are_cataloged_as_optional_integrations() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-pdf"] == {
        "distribution": "graphblocks-pdf",
        "import": "graphblocks_pdf",
        "default": False,
        "layer": "document_parser_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }


def test_pdf_parser_package_has_lazy_optional_parser_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-pdf"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-pdf"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any("pypdf" in dependency.lower() or "pdfminer" in dependency.lower() for dependency in dependencies)
    assert pyproject["project"]["optional-dependencies"]["pypdf"] == ["pypdf>=4.0"]
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_pdf"
    ]
    assert (package_root / "src" / "graphblocks_pdf" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_pdf" / "py.typed").exists()


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


def test_vector_store_adapter_packages_are_cataloged_as_optional_integrations() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-qdrant"] == {
        "distribution": "graphblocks-qdrant",
        "import": "graphblocks_qdrant",
        "default": False,
        "layer": "retrieval_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }


def test_vector_store_adapter_packages_have_pure_python_layouts_without_sdk_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-qdrant"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-qdrant"
    assert dependencies == ["graphblocks-rag~=1.0"]
    assert not any(
        vector_client in dependency.lower()
        for dependency in dependencies
        for vector_client in ("qdrant", "requests", "httpx", "grpc")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_qdrant"
    ]
    assert (package_root / "src" / "graphblocks_qdrant" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_qdrant" / "py.typed").exists()


def test_framework_bridge_packages_are_cataloged_as_optional_integrations() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-haystack"] == {
        "distribution": "graphblocks-haystack",
        "import": "graphblocks_haystack",
        "default": False,
        "layer": "framework_bridge",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }


def test_framework_bridge_packages_have_pure_python_layouts_without_framework_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-haystack"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-haystack"
    assert dependencies == ["graphblocks-core~=1.0"]
    assert not any(
        framework in dependency.lower()
        for dependency in dependencies
        for framework in ("haystack", "farm-haystack", "deepset")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_haystack"
    ]
    assert (package_root / "src" / "graphblocks_haystack" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_haystack" / "py.typed").exists()


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
    assert pyproject["project"]["scripts"] == {"graphblocks-tck": "graphblocks_testing:main"}
    assert (package_root / "src" / "graphblocks_testing" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_testing" / "py.typed").exists()


def test_devtools_package_has_pure_python_layout_without_graph_or_template_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-devtools"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-devtools"
    assert dependencies == ["graphblocks-core~=1.0", "graphblocks-cli~=1.0"]
    assert not any(
        dependency_name in dependency.lower()
        for dependency in dependencies
        for dependency_name in ("graphviz", "jinja", "networkx", "pydot")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_devtools"
    ]
    assert (package_root / "src" / "graphblocks_devtools" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_devtools" / "py.typed").exists()


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


def test_tui_package_has_pure_python_layout_without_ui_framework_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-tui"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-tui"
    assert dependencies == ["graphblocks-client~=1.0"]
    assert not any(
        framework in dependency.lower()
        for dependency in dependencies
        for framework in ("textual", "rich", "urwid", "prompt-toolkit")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_tui"
    ]
    assert (package_root / "src" / "graphblocks_tui" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_tui" / "py.typed").exists()


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


def test_kubernetes_package_has_pure_python_layout_without_client_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-kubernetes"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-kubernetes"
    assert dependencies == ["graphblocks-deployment~=1.0"]
    assert not any(
        client in dependency.lower()
        for dependency in dependencies
        for client in ("kubernetes", "openshift", "helm", "pyhelm", "kr8s")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_kubernetes"
    ]
    assert (package_root / "src" / "graphblocks_kubernetes" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_kubernetes" / "py.typed").exists()


def test_terraform_package_has_pure_python_layout_without_cli_or_hcl_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-terraform"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-terraform"
    assert dependencies == ["graphblocks-deployment~=1.0"]
    assert not any(
        tool in dependency.lower()
        for dependency in dependencies
        for tool in ("terraform", "hcl", "pulumi", "boto3", "google-cloud", "azure")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_terraform"
    ]
    assert (package_root / "src" / "graphblocks_terraform" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_terraform" / "py.typed").exists()


def test_oci_package_has_pure_python_layout_without_registry_client_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-oci"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-oci"
    assert dependencies == ["graphblocks-deployment~=1.0"]
    assert not any(
        client in dependency.lower()
        for dependency in dependencies
        for client in ("oras", "docker", "skopeo", "cosign", "cryptography")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_oci"
    ]
    assert (package_root / "src" / "graphblocks_oci" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_oci" / "py.typed").exists()


def test_gitops_package_has_pure_python_layout_without_controller_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-gitops"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-gitops"
    assert dependencies == ["graphblocks-deployment~=1.0"]
    assert not any(
        client in dependency.lower()
        for dependency in dependencies
        for client in ("kubernetes", "argocd", "flux", "gitpython", "dulwich")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_gitops"
    ]
    assert (package_root / "src" / "graphblocks_gitops" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_gitops" / "py.typed").exists()


def test_worker_package_has_pure_python_layout_without_server_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-worker"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-worker"
    assert dependencies == ["graphblocks-core~=1.0", "graphblocks-runtime~=1.0"]
    assert not any(
        framework in dependency.lower()
        for dependency in dependencies
        for framework in ("fastapi", "starlette", "django", "flask", "aiohttp")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_worker"
    ]
    assert (package_root / "src" / "graphblocks_worker" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_worker" / "py.typed").exists()


def test_server_package_has_pure_python_layout_without_web_framework_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-server"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-server"
    assert dependencies == ["graphblocks-core~=1.0", "graphblocks-runtime~=1.0"]
    assert not any(
        framework in dependency.lower()
        for dependency in dependencies
        for framework in ("fastapi", "starlette", "django", "flask", "aiohttp", "uvicorn")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_server"
    ]
    assert (package_root / "src" / "graphblocks_server" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_server" / "py.typed").exists()


def test_workspace_package_has_pure_python_layout_without_vcs_or_process_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-workspace"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-workspace"
    assert dependencies == ["graphblocks-core~=1.0", "graphblocks-policy~=1.0"]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("gitpython", "dulwich", "subprocess", "pytest")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_workspace"
    ]
    assert (package_root / "src" / "graphblocks_workspace" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_workspace" / "py.typed").exists()


def test_review_package_has_pure_python_layout_without_identity_provider_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-review"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-review"
    assert dependencies == ["graphblocks-core~=1.0", "graphblocks-policy~=1.0"]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("auth0", "okta", "ldap", "saml", "oauth")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_review"
    ]
    assert (package_root / "src" / "graphblocks_review" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_review" / "py.typed").exists()


def test_orchestration_package_has_pure_python_layout_without_provider_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-orchestration"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-orchestration"
    assert dependencies == [
        "graphblocks-core~=1.0",
        "graphblocks-policy~=1.0",
        "graphblocks-budget~=1.0",
    ]
    assert not any(
        provider in dependency.lower()
        for dependency in dependencies
        for provider in ("openai", "anthropic", "boto3", "google", "kubernetes")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_orchestration"
    ]
    assert (package_root / "src" / "graphblocks_orchestration" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_orchestration" / "py.typed").exists()


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
        assert dependencies == ["graphblocks-policy~=1.0"]
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


def test_observability_package_catalog_tracks_implemented_projection_surfaces() -> None:
    manifests = {manifest["distribution"]: manifest for manifest in load_package_catalog()["packages"]}

    assert manifests["graphblocks-telemetry"]["responsibility"] == [
        "canonical observation model",
        "output policy and tool execution telemetry records",
        "capture/redaction",
        "low-cardinality metric linting",
        "diagnostic bundle projection",
        "semantic mapping",
    ]
    assert manifests["graphblocks-otel"]["responsibility"] == [
        "generation/output policy/tool execution span projections",
        "OTLP exporter contracts",
        "collector templates",
    ]
    assert manifests["graphblocks-langfuse"]["responsibility"] == [
        "generation/output policy/tool execution event projections",
        "prompt/evaluation/dataset adapters",
    ]
    assert manifests["graphblocks-prometheus"]["responsibility"] == [
        "generation/output policy/tool execution metric projections",
        "recording_and_alert_rules",
        "metric cardinality lint integration",
    ]
    assert manifests["graphblocks-dashboards"]["responsibility"] == [
        "generation and policy/tool dashboard templates",
        "slo_rules",
        "runbook_templates",
    ]


def test_prometheus_package_has_pure_python_layout_without_client_dependency() -> None:
    package_root = ROOT / "packages" / "graphblocks-prometheus"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-prometheus"
    assert dependencies == ["graphblocks-telemetry~=1.0"]
    assert not any(
        client in dependency.lower()
        for dependency in dependencies
        for client in ("prometheus-client", "prometheus-api-client", "requests")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_prometheus"
    ]
    assert (package_root / "src" / "graphblocks_prometheus" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_prometheus" / "py.typed").exists()


def test_dashboards_package_has_data_package_layout_without_vendor_dependencies() -> None:
    package_root = ROOT / "packages" / "graphblocks-dashboards"
    pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert pyproject["project"]["name"] == "graphblocks-dashboards"
    assert dependencies == ["graphblocks-telemetry~=1.0"]
    assert not any(
        vendor in dependency.lower()
        for dependency in dependencies
        for vendor in ("grafana", "datadog", "newrelic", "requests")
    )
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/graphblocks_dashboards"
    ]
    assert (package_root / "src" / "graphblocks_dashboards" / "__init__.py").exists()
    assert (package_root / "src" / "graphblocks_dashboards" / "py.typed").exists()


def test_postgres_adapter_packages_have_sql_contract_layouts_without_db_driver_dependencies() -> None:
    cases = (
        ("graphblocks-budget-postgres", "graphblocks_budget_postgres", ["graphblocks-budget~=1.0"]),
        ("graphblocks-usage-postgres", "graphblocks_usage_postgres", ["graphblocks-usage~=1.0"]),
    )
    for distribution, import_name, expected_dependencies in cases:
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == expected_dependencies
        assert not any(
            driver in dependency.lower()
            for dependency in dependencies
            for driver in ("psycopg", "asyncpg", "sqlalchemy", "postgres")
        )
        assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            f"src/{import_name}"
        ]
        assert (package_root / "src" / import_name / "__init__.py").exists()
        assert (package_root / "src" / import_name / "py.typed").exists()


def test_durable_stream_adapter_packages_have_layouts_without_client_dependencies() -> None:
    cases = (
        ("graphblocks-kafka", "graphblocks_kafka", ("confluent-kafka", "kafka-python", "aiokafka")),
        ("graphblocks-nats", "graphblocks_nats", ("nats-py", "pynats", "asyncio-nats")),
        ("graphblocks-sqs", "graphblocks_sqs", ("boto3", "botocore", "aiobotocore", "aioboto3")),
        ("graphblocks-pubsub", "graphblocks_pubsub", ("google-cloud-pubsub", "google-api-core", "grpcio")),
    )
    for distribution, import_name, forbidden_clients in cases:
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == ["graphblocks-durable~=1.0"]
        assert not any(
            client in dependency.lower()
            for dependency in dependencies
            for client in (*forbidden_clients, "requests", "httpx")
        )
        assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            f"src/{import_name}"
        ]
        assert (package_root / "src" / import_name / "__init__.py").exists()
        assert (package_root / "src" / import_name / "py.typed").exists()


def test_voice_adapter_packages_have_layouts_without_media_sdk_dependencies() -> None:
    cases = (
        ("graphblocks-webrtc", "graphblocks_webrtc", ("aiortc", "webrtcvad", "av", "pylibsrtp")),
        ("graphblocks-websocket-media", "graphblocks_websocket_media", ("websockets", "aiohttp", "wsproto")),
        ("graphblocks-openai-realtime", "graphblocks_openai_realtime", ("openai", "websockets", "aiortc")),
        ("graphblocks-silero-vad", "graphblocks_silero_vad", ("torch", "onnxruntime", "torchaudio", "silero")),
    )
    for distribution, import_name, forbidden_clients in cases:
        package_root = ROOT / "packages" / distribution
        pyproject = tomllib.loads((package_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        assert pyproject["build-system"]["build-backend"] == "hatchling.build"
        assert pyproject["project"]["name"] == distribution
        assert dependencies == ["graphblocks-voice~=1.0"]
        assert not any(
            client in dependency.lower()
            for dependency in dependencies
            for client in (*forbidden_clients, "requests", "httpx", "websockets")
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


def test_package_lock_payload_and_digest_are_canonical() -> None:
    catalog = load_package_catalog()
    left = build_package_lock(
        catalog,
        requested=("graphblocks-openapi", "graphblocks-mcp"),
        include_default=False,
    )
    right = build_package_lock(
        catalog,
        requested=("graphblocks-mcp", "graphblocks-openapi"),
        include_default=False,
    )

    assert left.lock_payload()["packages"] == [
        {
            "default": True,
            "dependencies": [],
            "distribution": "graphblocks-core",
            "forbiddenDependencies": [],
            "import": "graphblocks",
            "kind": "pure_python",
            "layer": "schema_authoring",
            "stability": "foundation",
            "versionConstraint": "~=1.0",
        },
        {
            "default": False,
            "dependencies": ["graphblocks-core"],
            "distribution": "graphblocks-mcp",
            "forbiddenDependencies": [],
            "import": "graphblocks_mcp",
            "kind": "pure_python",
            "layer": "integration",
            "stability": "integration",
            "versionConstraint": None,
        },
        {
            "default": False,
            "dependencies": ["graphblocks-core"],
            "distribution": "graphblocks-openapi",
            "forbiddenDependencies": [],
            "import": "graphblocks_openapi",
            "kind": "pure_python",
            "layer": "integration",
            "stability": "integration",
            "versionConstraint": None,
        },
    ]
    assert left.lock_payload()["requested"] == ["graphblocks-mcp", "graphblocks-openapi"]
    assert left.content_digest().startswith("sha256:")
    assert left.content_digest() == right.content_digest()


def test_package_lock_rejects_selected_forbidden_transitive_dependency() -> None:
    catalog = {
        "catalogVersion": 1,
        "specVersion": "1.0",
        "defaultMetaPackage": {"distribution": "graphblocks", "dependencies": [], "excludedCategories": []},
        "packages": [
            {"distribution": "graphblocks", "default": True, "dependsOn": []},
            {
                "distribution": "graphblocks-documents",
                "default": False,
                "dependsOn": ["graphblocks-pdf"],
                "forbiddenDependencies": ["pypdf"],
            },
            {"distribution": "graphblocks-pdf", "default": False, "dependsOn": ["pypdf"]},
            {"distribution": "pypdf", "default": False, "dependsOn": []},
        ],
    }

    with pytest.raises(ValueError, match="forbidden package dependency"):
        build_package_lock(catalog, requested=("graphblocks-documents",), include_default=False)


def test_package_lock_records_validate_identity_types_and_uniqueness() -> None:
    entry = PackageLockEntry(
        distribution="graphblocks-core",
        version_constraint="~=1.0",
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
    with pytest.raises(ValueError, match="package lock entry distribution must not be empty"):
        PackageLockEntry(" ", None, None, True, None, None, None)
    with pytest.raises(ValueError, match="package lock entry default must be a boolean"):
        PackageLockEntry("graphblocks-core", None, None, "yes", None, None, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="package lock entry dependencies item must not be empty"):
        PackageLockEntry("graphblocks-core", None, None, True, None, None, None, dependencies=(" ",))

    lock = PackageLock(1, "1.0", requested=["graphblocks-core"], entries=[entry])  # type: ignore[arg-type]

    assert lock.requested == ("graphblocks-core",)
    assert lock.entries == (entry,)
    with pytest.raises(ValueError, match="package lock catalog_version must be positive"):
        PackageLock(0, "1.0", requested=("graphblocks-core",), entries=(entry,))
    with pytest.raises(ValueError, match="package lock entries must have unique distributions"):
        PackageLock(1, "1.0", requested=("graphblocks-core",), entries=(entry, entry))
    with pytest.raises(ValueError, match="package lock entries must be PackageLockEntry"):
        PackageLock(1, "1.0", requested=("graphblocks-core",), entries=(object(),))  # type: ignore[arg-type]


def test_package_catalog_doctor_accepts_builtin_catalog() -> None:
    diagnostics = doctor_package_catalog(load_package_catalog())

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


def test_package_catalog_doctor_cross_checks_local_pyproject_dependencies() -> None:
    diagnostics = doctor_package_catalog(load_package_catalog(), root=ROOT)

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


def test_package_wheel_matrix_covers_first_party_python_distributions() -> None:
    matrix = build_wheel_matrix(ROOT, python_versions=("3.11", "3.12"))
    targets = {target.distribution: target for target in matrix.targets}

    assert matrix.ok
    assert matrix.diagnostics == ()
    assert targets["graphblocks-core"].target_contract() == {
        "distribution": "graphblocks-core",
        "manifest": "pyproject.toml",
        "backend": "hatchling.build",
        "kind": "pure_python",
        "source_layout": "src/graphblocks",
        "python_versions": ["3.11", "3.12"],
    }
    assert targets["graphblocks-runtime"].kind == "native_extension"
    assert targets["graphblocks-runtime"].source_layout == "src"
    assert matrix.matrix_contract()["target_count"] == len(matrix.targets)
    assert matrix.content_digest().startswith("sha256:")
    assert "WheelMatrix" in __import__("graphblocks").__all__


def test_wheel_matrix_records_validate_identity_and_collection_types() -> None:
    target = WheelBuildTarget(
        distribution="graphblocks-core",
        manifest="pyproject.toml",
        backend="hatchling.build",
        kind="pure_python",
        source_layout="src/graphblocks",
        python_versions=["3.11", "3.12"],  # type: ignore[arg-type]
    )

    assert target.python_versions == ("3.11", "3.12")
    with pytest.raises(ValueError, match="wheel build target distribution must not be empty"):
        WheelBuildTarget(" ", "pyproject.toml", "hatchling.build", "pure_python", "src/graphblocks", ("3.11",))
    with pytest.raises(ValueError, match="invalid wheel build target kind"):
        WheelBuildTarget(
            "graphblocks-core",
            "pyproject.toml",
            "hatchling.build",
            "binary",  # type: ignore[arg-type]
            "src/graphblocks",
            ("3.11",),
        )
    with pytest.raises(ValueError, match="wheel build target python_versions item must not be empty"):
        WheelBuildTarget(
            "graphblocks-core",
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
        WheelMatrix(targets=(target,), diagnostics=(object(),))  # type: ignore[arg-type]


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
license = "Apache-2.0"
""".strip(),
        encoding="utf-8",
    )

    matrix = build_wheel_matrix(tmp_path, python_versions=("3.11", "3.12"))

    assert not matrix.ok
    assert [(item.code, item.path) for item in matrix.diagnostics] == [
        ("WheelBuildTargetMissing", "$.packages/broken-wheel/pyproject.toml.tool")
    ]


def test_package_catalog_doctor_reports_local_manifest_dependency_drift(tmp_path) -> None:
    package_root = tmp_path / "packages" / "graphblocks-agents"
    package_root.mkdir(parents=True)
    (package_root / "pyproject.toml").write_text(
        """
[project]
name = "graphblocks-agents"
version = "0.1.0"
dependencies = ["graphblocks-core~=1.0"]
""".strip(),
        encoding="utf-8",
    )

    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultMetaPackage": {"distribution": "graphblocks", "dependencies": [], "excludedCategories": []},
            "packages": [
                {"distribution": "graphblocks", "default": True, "dependsOn": []},
                {"distribution": "graphblocks-core", "default": True, "dependsOn": []},
                {"distribution": "graphblocks-policy", "default": True, "dependsOn": ["graphblocks-core"]},
                {
                    "distribution": "graphblocks-agents",
                    "default": False,
                    "dependsOn": ["graphblocks-policy"],
                },
            ],
        },
        root=tmp_path,
    )

    assert [(item.code, item.message) for item in diagnostics.diagnostics] == [
        (
            "PackageManifestDependencyMissing",
            "package manifest for 'graphblocks-agents' is missing catalog dependency 'graphblocks-policy'",
        ),
        (
            "PackageManifestDependencyUnexpected",
            "package manifest for 'graphblocks-agents' declares uncataloged first-party dependency 'graphblocks-core'",
        ),
    ]


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


def test_package_catalog_doctor_reports_forbidden_dependency_conflicts() -> None:
    diagnostics = doctor_package_catalog(
        {
            "catalogVersion": 1,
            "specVersion": "1.0",
            "defaultMetaPackage": {"distribution": "graphblocks", "dependencies": [], "excludedCategories": []},
            "packages": [
                {"distribution": "graphblocks", "default": True, "dependsOn": []},
                {
                    "distribution": "graphblocks-openai-realtime",
                    "default": False,
                    "dependsOn": ["graphblocks-voice", "openai"],
                    "forbiddenDependencies": ["openai"],
                },
                {"distribution": "graphblocks-voice", "default": False, "dependsOn": []},
                {"distribution": "openai", "default": False, "dependsOn": []},
            ],
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["PackageForbiddenDependencySelected"]
    assert "openai" in diagnostics.diagnostics[0].message


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


def test_package_manifest_audit_accepts_repo_manifest_licenses() -> None:
    diagnostics = audit_package_manifests(ROOT)

    assert diagnostics.ok
    assert diagnostics.diagnostics == ()


def test_package_manifest_audit_policy_validates_and_normalizes_string_collections() -> None:
    policy = PackageManifestAuditPolicy(
        allowed_licenses=("MIT", "Apache-2.0", "MIT"),
        blocked_dependencies=("vulnerable_sdk", "Vulnerable-SDK"),
    )

    assert policy.allowed_licenses == ("Apache-2.0", "MIT")
    assert policy.blocked_dependencies == ("vulnerable-sdk",)
    with pytest.raises(ValueError, match="package manifest audit policy allowed_licenses item must not be empty"):
        PackageManifestAuditPolicy(allowed_licenses=(" ",))
    with pytest.raises(ValueError, match="package manifest audit policy blocked_dependencies item must be a string"):
        PackageManifestAuditPolicy(blocked_dependencies=(object(),))  # type: ignore[arg-type]


def test_package_manifest_audit_reports_denied_license_and_blocked_dependency(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "unsafe-python"
version = "0.1.0"
license = "Proprietary"
dependencies = ["safe>=1", "vulnerable-sdk>=0"]
""".strip(),
        encoding="utf-8",
    )
    crate = tmp_path / "crates" / "unsafe-rust"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        """
[package]
name = "unsafe-rust"
version = "0.1.0"
license = "Apache-2.0"

[dependencies]
vulnerable-crate = "0.1"
""".strip(),
        encoding="utf-8",
    )

    diagnostics = audit_package_manifests(
        tmp_path,
        policy=PackageManifestAuditPolicy(blocked_dependencies=("vulnerable-sdk", "vulnerable-crate")),
    )

    assert [(item.code, item.path) for item in diagnostics.diagnostics] == [
        ("PackageLicenseDenied", "$.pyproject.toml.project.license"),
        ("PackageBlockedDependency", "$.pyproject.toml.project.dependencies[1]"),
        ("PackageBlockedDependency", "$.crates/unsafe-rust/Cargo.toml.dependencies.vulnerable-crate"),
    ]
