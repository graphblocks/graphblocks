from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def load_runtime_wrapper():
    package_root = ROOT / "packages" / "graphblocks-runtime" / "src" / "graphblocks_runtime"
    module_name = "_graphblocks_runtime_wrapper_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_runtime_wrapper_reports_native_binding_readiness_without_second_implementation() -> None:
    runtime = load_runtime_wrapper()

    assert runtime.native_extension_available() is False
    status = runtime.native_extension_status()
    assert status == {
        "available": False,
        "binding_crate": "graphblocks-python",
        "binding_version": None,
        "module": "graphblocks_runtime._native",
        "error": status["error"],
    }
    assert "_native" in status["error"]
    assert "native_extension_available" in runtime.__all__
    assert "native_extension_status" in runtime.__all__
    assert "require_native_extension" in runtime.__all__
    with pytest.raises(RuntimeError, match="single PyO3 binding crate graphblocks-python"):
        runtime.require_native_extension()
