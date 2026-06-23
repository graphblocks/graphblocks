from __future__ import annotations

from pathlib import Path
import tomllib


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
