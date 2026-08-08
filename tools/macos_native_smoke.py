#!/usr/bin/env python3
"""Produce and verify installed macOS native-wheel smoke evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
from importlib.metadata import version as distribution_version
import json
from pathlib import Path
import platform
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_RUNNER = "macos-15"
SUPPORTED_PYTHON = frozenset({"3.11", "3.12"})
REQUIRED_CAPABILITIES = (
    "canonical.json.v1",
    "compiler.graph.v1",
    "protocol.application.v1",
    "protocol.worker.v1",
    "schema.identity.v1",
)
NATIVE_CANONICAL_SMOKE_JSON = '{"a":1,"b":2}'
NATIVE_CANONICAL_SMOKE_HASH = (
    "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
)
NATIVE_SCHEMA_ID_SMOKE = {
    "canonical": "schemas/Message@4294967295",
    "majorVersion": 4_294_967_295,
    "name": "schemas/Message",
}


class MacosSmokeError(ValueError):
    """Raised when macOS smoke evidence is incomplete or inconsistent."""


def _exact_text(value: object, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MacosSmokeError(f"{owner} must be exact non-empty text")
    return value


def _closed_mapping(
    value: object,
    fields: set[str],
    owner: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise MacosSmokeError(f"{owner} must contain exactly {sorted(fields)!r}")
    return value


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_probe(*, runner_label: str) -> dict[str, object]:
    """Execute the installed native binding and capture its platform identity."""

    import graphblocks
    import graphblocks_runtime
    import graphblocks_runtime._native as native

    graphblocks_runtime.require_native_extension()
    status = graphblocks_runtime.native_extension_status()
    status = _closed_mapping(
        status,
        {
            "available",
            "binding_crate",
            "binding_version",
            "binding_protocol_version",
            "capabilities",
            "module",
            "error",
        },
        "native extension status",
    )
    document = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "macos-native-smoke"},
        "spec": {"nodes": {"extension": {"block": "smoke.extension@1"}}},
    }
    compiled_bytes = graphblocks_runtime.compile_graph_json(
        json.dumps(document, separators=(",", ":"), sort_keys=True),
        allow_unknown_blocks=True,
    ).encode("utf-8")
    try:
        compiled = json.loads(compiled_bytes)
    except json.JSONDecodeError as error:
        raise MacosSmokeError("native compiler smoke returned invalid JSON") from error
    if type(compiled) is not dict or compiled.get("ok") is not True:
        raise MacosSmokeError("native compiler smoke did not produce a valid plan")
    diagnostics = compiled.get("diagnostics")
    if type(diagnostics) is not list:
        raise MacosSmokeError("native compiler smoke diagnostics are invalid")

    capabilities = status["capabilities"]
    if type(capabilities) not in {list, tuple}:
        raise MacosSmokeError("native extension capabilities are invalid")
    graphblocks_file = Path(graphblocks.__file__ or "")
    runtime_file = Path(graphblocks_runtime.__file__ or "")
    native_file = Path(native.__file__ or "")
    if not all(
        path.is_file() for path in (graphblocks_file, runtime_file, native_file)
    ):
        raise MacosSmokeError("installed module identity is unavailable")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "schemaVersion": 1,
        "runnerLabel": runner_label,
        "os": platform.system(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": {
            "implementation": platform.python_implementation(),
            "version": python_version,
            "executable": sys.executable,
            "prefix": sys.prefix,
            "basePrefix": sys.base_prefix,
        },
        "distributions": {
            "graphblocks": distribution_version("graphblocks"),
            "graphblocks-runtime": distribution_version("graphblocks-runtime"),
        },
        "modules": {
            "graphblocks": str(graphblocks_file.resolve()),
            "graphblocksRuntime": str(runtime_file.resolve()),
            "native": str(native_file.resolve()),
        },
        "nativeBinding": {
            "available": status["available"],
            "bindingCrate": status["binding_crate"],
            "bindingVersion": status["binding_version"],
            "bindingProtocolVersion": status["binding_protocol_version"],
            "capabilities": list(capabilities),
            "module": status["module"],
            "error": status["error"],
        },
        "canonicalSmoke": {
            "hash": graphblocks_runtime.canonical_hash_json('{"b":2,"a":1}'),
            "json": graphblocks_runtime.canonicalize_json('{"b":2,"a":1}'),
        },
        "schemaIdSmoke": graphblocks_runtime.parse_schema_id(
            "schemas/Message@4294967295"
        ),
        "compilerSmoke": {
            "ok": True,
            "diagnosticCount": len(diagnostics),
            "outputSha256": hashlib.sha256(compiled_bytes).hexdigest(),
        },
    }


def validate_probe(
    payload: object,
    *,
    expected_runner: str,
    expected_python: str,
) -> dict[str, object]:
    """Validate that a probe came from the selected installed macOS environment."""

    probe = _closed_mapping(
        payload,
        {
            "canonicalSmoke",
            "schemaVersion",
            "runnerLabel",
            "schemaIdSmoke",
            "os",
            "machine",
            "platform",
            "python",
            "distributions",
            "modules",
            "nativeBinding",
            "compilerSmoke",
        },
        "macOS native probe",
    )
    if expected_runner != SUPPORTED_RUNNER or expected_python not in SUPPORTED_PYTHON:
        raise MacosSmokeError("requested smoke runner or Python version is unsupported")
    if probe["schemaVersion"] != 1:
        raise MacosSmokeError("macOS native probe schema version is unsupported")
    expected_identity = {
        "runnerLabel": expected_runner,
        "os": "Darwin",
        "machine": "arm64",
    }
    for field, expected in expected_identity.items():
        if probe[field] != expected:
            raise MacosSmokeError(
                f"macOS native probe {field} mismatch: expected {expected!r}"
            )
    _exact_text(probe["platform"], "macOS native probe platform")

    python_identity = _closed_mapping(
        probe["python"],
        {"implementation", "version", "executable", "prefix", "basePrefix"},
        "macOS native probe Python identity",
    )
    if (
        python_identity["implementation"] != "CPython"
        or python_identity["version"] != expected_python
    ):
        raise MacosSmokeError("macOS native probe Python identity is unsupported")
    prefix = Path(_exact_text(python_identity["prefix"], "Python prefix"))
    base_prefix = Path(_exact_text(python_identity["basePrefix"], "Python base prefix"))
    executable = Path(_exact_text(python_identity["executable"], "Python executable"))
    if prefix == base_prefix or not executable.is_relative_to(prefix):
        raise MacosSmokeError(
            "macOS native probe did not run in the isolated environment"
        )

    distributions = _closed_mapping(
        probe["distributions"],
        {"graphblocks", "graphblocks-runtime"},
        "macOS native probe distributions",
    )
    expected_versions = {
        "graphblocks": tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"],
        "graphblocks-runtime": tomllib.loads(
            (ROOT / "packages/graphblocks-runtime/pyproject.toml").read_text(
                encoding="utf-8"
            )
        )["project"]["version"],
    }
    if distributions != expected_versions:
        raise MacosSmokeError(
            "macOS native probe distribution versions do not match source"
        )

    modules = _closed_mapping(
        probe["modules"],
        {"graphblocks", "graphblocksRuntime", "native"},
        "macOS native probe modules",
    )
    for field, raw_path in modules.items():
        module_path = Path(_exact_text(raw_path, f"installed module {field}"))
        if not module_path.is_file() or not module_path.is_relative_to(prefix):
            raise MacosSmokeError(
                f"installed module {field} is outside the smoke environment"
            )
    if not str(modules["native"]).endswith(".so"):
        raise MacosSmokeError(
            "macOS native extension does not have a shared-object suffix"
        )

    binding = _closed_mapping(
        probe["nativeBinding"],
        {
            "available",
            "bindingCrate",
            "bindingVersion",
            "bindingProtocolVersion",
            "capabilities",
            "module",
            "error",
        },
        "macOS native binding",
    )
    if binding != {
        "available": True,
        "bindingCrate": "graphblocks-python",
        "bindingVersion": expected_versions["graphblocks-runtime"],
        "bindingProtocolVersion": 1,
        "capabilities": list(REQUIRED_CAPABILITIES),
        "module": "graphblocks_runtime._native",
        "error": None,
    }:
        raise MacosSmokeError("macOS native binding contract does not match")

    canonical = _closed_mapping(
        probe["canonicalSmoke"],
        {"hash", "json"},
        "macOS native canonical smoke",
    )
    if canonical != {
        "hash": NATIVE_CANONICAL_SMOKE_HASH,
        "json": NATIVE_CANONICAL_SMOKE_JSON,
    }:
        raise MacosSmokeError("macOS native canonical smoke does not match")

    if probe["schemaIdSmoke"] != NATIVE_SCHEMA_ID_SMOKE:
        raise MacosSmokeError("macOS native schema id smoke does not match")

    compiler = _closed_mapping(
        probe["compilerSmoke"],
        {"ok", "diagnosticCount", "outputSha256"},
        "macOS native compiler smoke",
    )
    if (
        compiler["ok"] is not True
        or type(compiler["diagnosticCount"]) is not int
        or compiler["diagnosticCount"] < 0
        or type(compiler["outputSha256"]) is not str
        or len(compiler["outputSha256"]) != 64
    ):
        raise MacosSmokeError("macOS native compiler smoke evidence is invalid")
    return probe


def verify_wheelhouse(wheelhouse: Path) -> list[dict[str, object]]:
    """Bind the exact base and native wheels used by the smoke environment."""

    wheels = sorted(wheelhouse.glob("*.whl"))
    base_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    runtime_version = tomllib.loads(
        (ROOT / "packages/graphblocks-runtime/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]["version"]
    expected_prefixes = {
        "graphblocks": f"graphblocks-{base_version}-",
        "graphblocks-runtime": f"graphblocks_runtime-{runtime_version}-",
    }
    if len(wheels) != 2:
        raise MacosSmokeError("macOS smoke wheelhouse must contain exactly two wheels")
    artifacts: list[dict[str, object]] = []
    for distribution, prefix in expected_prefixes.items():
        matches = [wheel for wheel in wheels if wheel.name.startswith(prefix)]
        if len(matches) != 1:
            raise MacosSmokeError(f"macOS smoke wheel for {distribution} is not exact")
        wheel = matches[0]
        if distribution == "graphblocks-runtime" and not (
            "abi3" in wheel.name and "macosx" in wheel.name and "arm64" in wheel.name
        ):
            raise MacosSmokeError("native macOS wheel tags are not abi3 arm64")
        content = wheel.read_bytes()
        artifacts.append(
            {
                "distribution": distribution,
                "filename": wheel.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    return artifacts


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--runner-label", required=True)
    probe.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--probe", type=Path, required=True)
    verify.add_argument("--wheelhouse", type=Path, required=True)
    verify.add_argument("--expected-runner", required=True)
    verify.add_argument("--expected-python", required=True)
    verify.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "probe":
            _write_json(args.output, create_probe(runner_label=args.runner_label))
            return 0
        probe = json.loads(args.probe.read_text(encoding="utf-8"))
        validated_probe = validate_probe(
            probe,
            expected_runner=args.expected_runner,
            expected_python=args.expected_python,
        )
        evidence: dict[str, object] = {
            "schemaVersion": 1,
            "classification": "smoke-only",
            "changesSupportedPlatformMatrix": False,
            "probe": validated_probe,
            "artifacts": verify_wheelhouse(args.wheelhouse),
        }
        evidence["contentDigest"] = hashlib.sha256(
            json.dumps(evidence, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        _write_json(args.output, evidence)
        print(
            "macOS native-wheel smoke evidence passed for "
            f"{args.expected_runner} Python {args.expected_python}"
        )
        return 0
    except (MacosSmokeError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"macOS native-wheel smoke failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
