from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from graphblocks import canonical


_DIRECT_CANONICAL_IMPORT_ALLOWLIST = frozenset(
    {
        "__init__.py",
        "_canonical_reference.py",
        "_outcome_reference.py",
    }
)


def test_python_internals_use_the_explicit_canonical_reference_boundary() -> None:
    package_root = Path(canonical.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.glob("*.py")):
        if path.name in _DIRECT_CANONICAL_IMPORT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "canonical"
            for node in ast.walk(tree)
        ):
            offenders.append(path.name)

    assert offenders == []


def test_public_canonical_facade_dispatches_to_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def canonicalize_json(value: str) -> str:
        calls.append(("json", value))
        return '{"a":1,"b":2}'

    def canonical_hash_json(value: str) -> str:
        calls.append(("hash", value))
        return "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            canonical_hash_json=canonical_hash_json,
            canonicalize_json=canonicalize_json,
            native_extension_available=lambda: True,
        ),
    )
    value = {"b": 2, "a": 1}

    assert canonical.canonical_dumps(value) == '{"a":1,"b":2}'
    assert canonical.canonical_hash(value) == (
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert canonical.canonical_loads(b'{"b":2,"a":1}') == {"a": 1, "b": 2}
    assert calls == [
        ("json", '{"a":1,"b":2}'),
        ("hash", '{"a":1,"b":2}'),
        ("json", '{"b":2,"a":1}'),
    ]


def test_public_canonical_facade_fails_closed_without_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: False,
            native_extension_status=lambda: {"error": "binding unavailable"},
        ),
    )

    with pytest.raises(
        canonical.NativeCanonicalUnavailableError,
        match=r"canonical_\*_reference APIs: binding unavailable",
    ):
        canonical.canonical_dumps({"ok": True})
    assert canonical.canonical_dumps_reference({"ok": True}) == '{"ok":true}'
    assert canonical.canonical_hash_reference({"ok": True}).startswith("sha256:")
    assert canonical.canonical_loads_reference('{"ok":true}') == {"ok": True}


@pytest.mark.parametrize(
    ("function_name", "result", "message"),
    (
        ("canonicalize_json", object(), "canonicalize_json must return a string"),
        ("canonical_hash_json", "sha256:INVALID", "canonical sha256 digest"),
        (
            "canonicalize_json",
            "true",
            "result differs from the reference oracle",
        ),
        (
            "canonical_hash_json",
            "sha256:" + "0" * 64,
            "result differs from the reference oracle",
        ),
    ),
)
def test_public_canonical_facade_rejects_invalid_native_results(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    result: object,
    message: str,
) -> None:
    native = SimpleNamespace(
        canonical_hash_json=lambda value: (
            "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
        ),
        canonicalize_json=lambda value: "null",
        native_extension_available=lambda: True,
    )
    setattr(native, function_name, lambda value: result)
    monkeypatch.setitem(sys.modules, "graphblocks_runtime", native)

    with pytest.raises(canonical.NativeCanonicalContractError, match=message):
        if function_name == "canonicalize_json":
            canonical.canonical_dumps(None)
        else:
            canonical.canonical_hash(None)


@pytest.mark.parametrize(
    ("attribute", "message"),
    (
        (
            "native_extension_available",
            "native extension availability check failed",
        ),
        ("native_extension_status", "native extension status check failed"),
    ),
)
def test_public_canonical_facade_closes_failed_native_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    message: str,
) -> None:
    native = SimpleNamespace(
        canonicalize_json=lambda value: value,
        native_extension_available=lambda: False,
        native_extension_status=lambda: {"error": "binding unavailable"},
    )

    def fail() -> object:
        raise RuntimeError("hostile handshake")

    setattr(native, attribute, fail)
    monkeypatch.setitem(sys.modules, "graphblocks_runtime", native)

    with pytest.raises(canonical.NativeCanonicalUnavailableError, match=message):
        canonical.canonical_dumps(None)


def test_public_canonical_facade_rejects_native_errors_for_valid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(value: str) -> str:
        raise ValueError("native rejected input")

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            canonicalize_json=fail,
            native_extension_available=lambda: True,
        ),
    )

    with pytest.raises(
        canonical.NativeCanonicalContractError,
        match="rejected reference-valid JSON",
    ):
        canonical.canonical_dumps(None)
