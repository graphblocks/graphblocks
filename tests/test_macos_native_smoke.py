from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).parents[1]
TOOL_PATH = ROOT / "tools/macos_native_smoke.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("macos_native_smoke", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _probe(tmp_path: Path) -> dict[str, object]:
    prefix = tmp_path / "venv"
    executable = prefix / "bin/python"
    graphblocks_module = prefix / "lib/python3.11/site-packages/graphblocks/__init__.py"
    runtime_module = (
        prefix / "lib/python3.11/site-packages/graphblocks_runtime/__init__.py"
    )
    native_module = (
        prefix / "lib/python3.11/site-packages/graphblocks_runtime/_native.abi3.so"
    )
    for path in (executable, graphblocks_module, runtime_module, native_module):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installed-smoke-fixture")
    return {
        "schemaVersion": 1,
        "runnerLabel": "macos-15",
        "os": "Darwin",
        "machine": "arm64",
        "platform": "macOS-15-arm64",
        "python": {
            "implementation": "CPython",
            "version": "3.11",
            "executable": str(executable),
            "prefix": str(prefix),
            "basePrefix": "/Library/Frameworks/Python.framework/Versions/3.11",
        },
        "distributions": {
            "graphblocks": "1.0.0rc7",
            "graphblocks-runtime": "0.1.0",
        },
        "modules": {
            "graphblocks": str(graphblocks_module),
            "graphblocksRuntime": str(runtime_module),
            "native": str(native_module),
        },
        "nativeBinding": {
            "available": True,
            "bindingCrate": "graphblocks-python",
            "bindingVersion": "0.1.0",
            "bindingProtocolVersion": 1,
            "capabilities": [
                "canonical.json.v1",
                "compiler.graph.v1",
                "protocol.application.v1",
                "protocol.worker.v1",
                "runtime.local.v1",
                "schema.identity.v1",
                "schema.resource-migration.v1",
                "schema.resource-validation.v1",
            ],
            "module": "graphblocks_runtime._native",
            "error": None,
        },
        "canonicalSmoke": {
            "hash": "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
            "json": '{"a":1,"b":2}',
        },
        "schemaIdSmoke": {
            "canonical": "schemas/Message@4294967295",
            "majorVersion": 4_294_967_295,
            "name": "schemas/Message",
        },
        "compilerSmoke": {
            "ok": True,
            "diagnosticCount": 0,
            "outputSha256": "a" * 64,
        },
    }


def test_macos_probe_is_closed_installed_and_native(tmp_path: Path) -> None:
    tool = _load_tool()
    probe = _probe(tmp_path)

    assert (
        tool.validate_probe(
            probe,
            expected_runner="macos-15",
            expected_python="3.11",
        )
        == probe
    )

    wrong_os = deepcopy(probe)
    wrong_os["os"] = "Linux"
    with pytest.raises(tool.MacosSmokeError, match="os mismatch"):
        tool.validate_probe(
            wrong_os,
            expected_runner="macos-15",
            expected_python="3.11",
        )

    source_import = deepcopy(probe)
    source_import["modules"]["graphblocks"] = str(ROOT / "src/graphblocks/__init__.py")
    with pytest.raises(tool.MacosSmokeError, match="outside the smoke environment"):
        tool.validate_probe(
            source_import,
            expected_runner="macos-15",
            expected_python="3.11",
        )

    wrong_canonical = deepcopy(probe)
    wrong_canonical["canonicalSmoke"]["json"] = '{"b":2,"a":1}'
    with pytest.raises(tool.MacosSmokeError, match="canonical smoke does not match"):
        tool.validate_probe(
            wrong_canonical,
            expected_runner="macos-15",
            expected_python="3.11",
        )

    wrong_schema_id = deepcopy(probe)
    wrong_schema_id["schemaIdSmoke"]["majorVersion"] = 1
    with pytest.raises(tool.MacosSmokeError, match="schema id smoke does not match"):
        tool.validate_probe(
            wrong_schema_id,
            expected_runner="macos-15",
            expected_python="3.11",
        )


def test_macos_wheelhouse_requires_exact_arm64_abi3_artifacts(tmp_path: Path) -> None:
    tool = _load_tool()
    base = tmp_path / "graphblocks-1.0.0rc7-py3-none-any.whl"
    native = tmp_path / "graphblocks_runtime-0.1.0-cp311-abi3-macosx_11_0_arm64.whl"
    base.write_bytes(b"base-wheel")
    native.write_bytes(b"native-wheel")

    assert tool.verify_wheelhouse(tmp_path) == [
        {
            "distribution": "graphblocks",
            "filename": base.name,
            "sha256": hashlib.sha256(b"base-wheel").hexdigest(),
            "size": 10,
        },
        {
            "distribution": "graphblocks-runtime",
            "filename": native.name,
            "sha256": hashlib.sha256(b"native-wheel").hexdigest(),
            "size": 12,
        },
    ]

    native.rename(tmp_path / "graphblocks_runtime-0.1.0-cp311-abi3-linux_x86_64.whl")
    with pytest.raises(tool.MacosSmokeError, match="abi3 arm64"):
        tool.verify_wheelhouse(tmp_path)


def test_macos_native_wheel_smoke_is_required_for_both_python_versions() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["macos-native-smoke"]
    assert job["runs-on"] == "macos-15"
    assert job["env"] == {"RUSTUP_TOOLCHAIN": "1.94.0"}
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {"python-version": ["3.11", "3.12"]},
    }
    steps = {step["name"]: step for step in job["steps"]}
    native_build = steps["Build locked native wheel"]
    assert native_build["working-directory"] == "packages/graphblocks-runtime"
    assert "python -m maturin build --release --locked" in native_build["run"]
    probe = steps["Execute installed native compiler smoke"]["run"]
    assert "dist/macos-smoke-venv/bin/python" in probe
    assert "tools/macos_native_smoke.py probe" in probe
    assert "tools/macos_native_smoke.py verify" in probe
    retained = steps["Retain macOS native-wheel smoke evidence"]
    assert retained["if"] == "always()"
    assert retained["with"]["if-no-files-found"] == "error"

    required = workflow["jobs"]["required-gates"]
    assert "macos-native-smoke" in required["needs"]
    required_step = required["steps"][0]
    assert required_step["env"]["MACOS_NATIVE_SMOKE_RESULT"] == (
        "${{ needs.macos-native-smoke.result }}"
    )
    assert "macos-native-smoke:$MACOS_NATIVE_SMOKE_RESULT" in required_step["run"]
