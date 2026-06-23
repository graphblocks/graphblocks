from __future__ import annotations

from pathlib import Path
import tomllib

from graphblocks.packages import build_package_lock, load_package_catalog, package_rows


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
