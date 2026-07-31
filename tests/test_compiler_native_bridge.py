from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError
import inspect
import math
import sys
from types import SimpleNamespace

import pytest

from graphblocks import compiler as compiler_module
from graphblocks.canonical import canonical_hash
from graphblocks.compiler import (
    NativeCompilerContractError,
    NativeCompilerUnavailableError,
    Plan,
    compile_graph,
    compile_graph_native,
    compile_graph_native_plan,
    compile_graph_reference,
)
from graphblocks.plugins import BlockCatalog


NORMALIZED_GRAPH: dict[str, object] = {
    "apiVersion": "graphblocks.ai/v1",
    "kind": "Graph",
    "metadata": {"name": "native-plan"},
    "spec": {"edges": [], "nodes": {}},
}


def _valid_native_result() -> dict[str, object]:
    return {
        "diagnostics": [],
        "graph": NORMALIZED_GRAPH,
        "hash": canonical_hash(NORMALIZED_GRAPH),
        "ok": True,
    }


def test_compile_graph_reference_is_the_explicit_python_entrypoint() -> None:
    catalog = BlockCatalog({}, allow_unknown_blocks=True)

    reference_plan = compile_graph_reference(
        NORMALIZED_GRAPH,
        block_catalog=catalog,
    )

    assert reference_plan.ok
    assert reference_plan.normalized == NORMALIZED_GRAPH


def test_reference_compiler_phase_golden_fixture() -> None:
    decoded = compiler_module._decode_graph_phase(
        NORMALIZED_GRAPH,
        BlockCatalog({}, allow_unknown_blocks=True),
        allow_unknown_blocks=False,
    )
    assert not isinstance(decoded, Plan)
    assert decoded.document == NORMALIZED_GRAPH
    assert decoded.document is not NORMALIZED_GRAPH
    with pytest.raises(FrozenInstanceError):
        decoded.api_version = "changed"  # type: ignore[misc]

    catalog = compiler_module._catalog_graph_phase(decoded)
    migrated = compiler_module._migrate_graph_phase(catalog)
    validated = compiler_module._validate_graph_phase(migrated)
    assert not isinstance(validated, Plan)
    normalized = compiler_module._normalize_graph_phase(validated)
    typechecked = compiler_module._typecheck_graph_phase(normalized)
    lowered = compiler_module._lower_graph_phase(typechecked)
    plan = compiler_module._evidence_graph_phase(lowered)

    assert decoded.diagnostics == ()
    assert validated.diagnostics == ()
    assert typechecked.diagnostics == ()
    assert plan.to_dict() == {
        "diagnostics": [],
        "graph": NORMALIZED_GRAPH,
        "hash": "sha256:9e2ec3d84827074143199e9064687eb3aa658f124750f38d8056831685308d69",
        "ok": True,
    }


def test_reference_compiler_runs_declared_phases_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    for phase_name in compiler_module._REFERENCE_COMPILER_PHASE_ORDER:
        attribute = f"_{phase_name}_graph_phase"
        original = getattr(compiler_module, attribute)

        def observe(
            *args: object,
            _name: str = phase_name,
            _original: Callable[..., object] = original,
            **kwargs: object,
        ) -> object:
            observed.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(compiler_module, attribute, observe)

    plan = compile_graph_reference(
        NORMALIZED_GRAPH,
        block_catalog=BlockCatalog({}, allow_unknown_blocks=True),
    )

    assert plan.ok
    assert tuple(observed) == compiler_module._REFERENCE_COMPILER_PHASE_ORDER


def test_reference_compiler_entrypoint_is_only_a_phase_orchestrator() -> None:
    source = inspect.getsource(compile_graph_reference)
    function = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
    )

    assert function.end_lineno is not None
    assert function.end_lineno - function.lineno + 1 <= 30
    assert not any(isinstance(node, ast.For) for node in ast.walk(function))


def test_compile_graph_dispatches_to_the_native_plan_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = BlockCatalog({}, allow_unknown_blocks=True)
    expected = compile_graph_reference(
        NORMALIZED_GRAPH,
        block_catalog=catalog,
    )
    calls: list[tuple[dict[str, object], BlockCatalog | None, bool]] = []

    def native_plan(
        document: dict[str, object],
        block_catalog: BlockCatalog | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> Plan:
        calls.append((document, block_catalog, allow_unknown_blocks))
        return expected

    monkeypatch.setattr(
        compiler_module,
        "compile_graph_native_plan",
        native_plan,
    )

    plan = compile_graph(
        NORMALIZED_GRAPH,
        block_catalog=catalog,
        allow_unknown_blocks=True,
    )

    assert plan is expected
    assert calls == [(NORMALIZED_GRAPH, catalog, True)]


def test_compile_graph_fails_closed_when_native_compiler_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_called = False
    native_called = False

    def unexpected_reference(*args: object, **kwargs: object) -> Plan:
        nonlocal reference_called
        reference_called = True
        raise AssertionError("reference compiler fallback must be explicit")

    def incompatible_native(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal native_called
        native_called = True
        return _valid_native_result()

    monkeypatch.setattr(
        compiler_module,
        "compile_graph_reference",
        unexpected_reference,
    )
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            compile_graph=incompatible_native,
            native_extension_available=lambda: False,
            native_extension_status=lambda: {
                "error": "unsupported native binding protocol version 2"
            },
        ),
    )

    with pytest.raises(
        NativeCompilerUnavailableError,
        match="unsupported native binding protocol version",
    ):
        compile_graph(NORMALIZED_GRAPH)

    assert reference_called is False
    assert native_called is False


def test_native_compile_helper_serializes_python_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict[str, object], object | None, bool]] = []

    def native_compile_graph(
        document: dict[str, object],
        block_catalog: object | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> dict[str, object]:
        calls.append((document, block_catalog, allow_unknown_blocks))
        return _valid_native_result()

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(compile_graph=native_compile_graph),
    )
    catalog = BlockCatalog.from_blocks(
        [{"typeId": "test.echo", "version": 1}],
        allow_unknown_blocks=True,
    )

    result = compile_graph_native(NORMALIZED_GRAPH, block_catalog=catalog)

    assert result == _valid_native_result()
    assert calls == [(NORMALIZED_GRAPH, catalog.to_blocks(), True)]


def test_native_compiler_result_is_restored_as_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _valid_native_result()
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(compile_graph=lambda *args, **kwargs: result),
    )

    plan = compile_graph_native_plan(
        NORMALIZED_GRAPH,
        block_catalog=BlockCatalog({}),
    )

    assert isinstance(plan, Plan)
    assert plan.to_dict() == result


@pytest.mark.parametrize(
    "invalid_value",
    [
        {1: "not a JSON object key"},
        "\ud800",
        {"\udfff": "value"},
        math.nan,
        math.inf,
        -math.inf,
        b"bytes",
        {"set"},
        object(),
    ],
    ids=(
        "non-string-key",
        "surrogate-string",
        "surrogate-key",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "bytes",
        "set",
        "object",
    ),
)
def test_native_plan_bridge_matches_reference_json_domain_diagnostics(
    invalid_value: object,
) -> None:
    document: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "native-invalid-json-domain"},
        "spec": {"nodes": {}, "extensions": invalid_value},
    }

    reference = compile_graph_reference(document)
    native = compile_graph_native_plan(document)

    assert native.to_dict() == reference.to_dict()


def test_native_plan_bridge_matches_reference_depth_diagnostic() -> None:
    nested: dict[str, object] = {}
    current = nested
    for _ in range(65):
        child: dict[str, object] = {}
        current["next"] = child
        current = child
    document: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "native-too-deep"},
        "spec": {"nodes": {}, "extensions": nested},
    }

    reference = compile_graph_reference(document)
    native = compile_graph_native_plan(document)

    assert native.to_dict() == reference.to_dict()


def test_native_plan_bridge_normalizes_hostile_mapping_errors() -> None:
    class ExplodingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("hostile lookup")

        def __iter__(self) -> Iterator[str]:
            return iter(("apiVersion",))

        def __len__(self) -> int:
            return 1

    with pytest.raises(
        ValueError,
        match="graph document must contain stable canonical JSON values",
    ) as captured:
        compile_graph_native_plan(ExplodingMapping())  # type: ignore[arg-type]

    assert isinstance(captured.value.__cause__, RuntimeError)


def test_native_plan_bridge_validates_public_input_types() -> None:
    with pytest.raises(TypeError, match="graph document must be a mapping"):
        compile_graph_native_plan([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="block_catalog must be a BlockCatalog"):
        compile_graph_native_plan(
            NORMALIZED_GRAPH,
            block_catalog=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="allow_unknown_blocks must be a boolean"):
        compile_graph_native_plan(
            NORMALIZED_GRAPH,
            allow_unknown_blocks=1,  # type: ignore[arg-type]
        )


def test_native_plan_bridge_accepts_a_python_block_catalog() -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.conditional",
                "version": 1,
                "outputs": [
                    {
                        "name": "value",
                        "type": "String",
                        "required": False,
                        "requiredWhen": {
                            "configEquals": {
                                "pointer": "/enabled",
                                "value": True,
                            }
                        },
                    }
                ],
                "resourceSlots": [
                    {
                        "name": "model",
                        "type": "providers.Model",
                        "optional": True,
                    }
                ],
                "configSchema": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    document: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "native-python-catalog"},
        "spec": {
            "nodes": {
                "conditional": {
                    "block": "test.conditional@1",
                    "config": {"enabled": True},
                }
            }
        },
    }

    reference = compile_graph_reference(document, block_catalog=catalog)
    native = compile_graph_native_plan(document, block_catalog=catalog)

    assert native.graph_hash == reference.graph_hash
    assert native.normalized == reference.normalized
    assert [
        (item.code, item.severity, item.path) for item in native.diagnostics.diagnostics
    ] == [
        (item.code, item.severity, item.path)
        for item in reference.diagnostics.diagnostics
    ]


@pytest.mark.parametrize(
    ("api_version", "block_id", "config"),
    [
        ("graphblocks.ai/v1", "agent.run@1", {}),
        ("graphblocks.ai/v1alpha3", "agent.run@1", {}),
        ("graphblocks.ai/v1", "control.map@2", {"graph": "nested"}),
        ("graphblocks.ai/v1alpha3", "control.map@2", {"graph": "nested"}),
        (
            "graphblocks.ai/v1",
            "control.map@2",
            {"block": "prompt.render@1"},
        ),
    ],
)
def test_native_plan_bridge_selects_the_builtin_catalog_profile(
    api_version: str,
    block_id: str,
    config: dict[str, object],
) -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )
    document: dict[str, object] = {
        "apiVersion": api_version,
        "kind": "Graph",
        "metadata": {"name": "native-catalog-profile"},
        "spec": {
            "nodes": {
                "selected": {
                    "block": block_id,
                    "config": config,
                }
            }
        },
    }

    reference = compile_graph_reference(document)
    native = compile_graph_native_plan(document)

    assert native.graph_hash == reference.graph_hash
    assert native.normalized == reference.normalized
    assert [
        (item.code, item.severity, item.path) for item in native.diagnostics.diagnostics
    ] == [
        (item.code, item.severity, item.path)
        for item in reference.diagnostics.diagnostics
    ]


def test_native_plan_bridge_rejects_holdback_duration_bounds() -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )
    document: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "native-holdback-duration"},
        "spec": {
            "nodes": {},
            "outputPolicy": {
                "delivery": {
                    "mode": "bounded_holdback",
                    "holdbackMaxDuration": "250ms",
                    "onViolation": "abort_response",
                },
                "evaluation": {
                    "enforcementPoints": [
                        "on_generation_chunk",
                        "before_client_delivery",
                        "before_output_commit",
                    ]
                },
            },
        },
    }

    reference = compile_graph_reference(document)
    native = compile_graph_native_plan(document)

    assert native.graph_hash == reference.graph_hash
    assert native.diagnostics.to_list() == reference.diagnostics.to_list()


@pytest.mark.parametrize(
    ("document", "catalog"),
    [
        (
            {
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {"name": "native-interface-diagnostic-quotes"},
                "spec": {
                    "interface": {
                        "inputs": {"declared": "String"},
                        "outputs": {"declared": "String"},
                    },
                    "nodes": {},
                    "edges": [
                        {
                            "from": "$input.missing",
                            "to": "$output.missing",
                        }
                    ],
                },
            },
            BlockCatalog({}, allow_unknown_blocks=True),
        ),
        (
            {
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {"name": "native-when-diagnostic-quotes"},
                "spec": {
                    "nodes": {
                        "source": {"block": "test.source@1"},
                        "branch": {
                            "block": "test.branch@1",
                            "when": "source.missing",
                        },
                    }
                },
            },
            BlockCatalog.from_blocks(
                [
                    {
                        "typeId": "test.source",
                        "version": 1,
                        "outputs": [{"name": "enabled", "type": "Boolean"}],
                    },
                    {"typeId": "test.branch", "version": 1},
                ]
            ),
        ),
        (
            {
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {"name": "native-block-diagnostic-quotes"},
                "spec": {
                    "nodes": {
                        "selected": {"block": "vendor.o'clock@1"},
                    }
                },
            },
            BlockCatalog.from_blocks([]),
        ),
    ],
)
def test_native_plan_bridge_matches_reference_diagnostic_quotes(
    document: dict[str, object],
    catalog: BlockCatalog,
) -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )

    reference = compile_graph_reference(document, block_catalog=catalog)
    native = compile_graph_native_plan(document, block_catalog=catalog)

    assert native.diagnostics.to_list() == reference.diagnostics.to_list()


@pytest.mark.parametrize(
    "api_version",
    ["graphblocks.ai/v2", ["bad"], None, 3, True],
)
def test_native_plan_bridge_matches_reference_api_version_diagnostics(
    api_version: object,
) -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )
    document: dict[str, object] = {
        "apiVersion": api_version,
        "kind": "Graph",
        "metadata": {"name": "native-api-version-diagnostic"},
        "spec": {"nodes": {}},
    }

    reference = compile_graph_reference(document)
    native = compile_graph_native_plan(document)

    assert native.diagnostics.to_list() == reference.diagnostics.to_list()


def test_native_plan_bridge_preserves_bounded_large_integers() -> None:
    pytest.importorskip(
        "graphblocks_runtime",
        reason="native Plan integration requires the Rust binding",
    )
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.large_integer",
                "version": 1,
                "configSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    value = 10**5_000
    document: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "native-large-integer"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "test.large_integer@1",
                    "config": {"value": value},
                }
            }
        },
    }

    reference = compile_graph_reference(document, block_catalog=catalog)
    native = compile_graph_native_plan(document, block_catalog=catalog)

    assert native.graph_hash == reference.graph_hash
    assert native.normalized == reference.normalized
    assert [
        (item.code, item.severity, item.path) for item in native.diagnostics.diagnostics
    ] == [
        (item.code, item.severity, item.path)
        for item in reference.diagnostics.diagnostics
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda result: result.__setitem__("extra", True),
            "must contain exactly",
        ),
        (
            lambda result: result.__setitem__("hash", "not-a-digest"),
            "canonical sha256 digest",
        ),
        (
            lambda result: result.__setitem__("hash", "sha256:" + "0" * 64),
            "does not match",
        ),
        (
            lambda result: result.__setitem__("diagnostics", {}),
            "diagnostics must be an array",
        ),
        (
            lambda result: result.__setitem__(
                "diagnostics",
                [
                    {
                        "code": "GB0001",
                        "message": "invalid",
                        "path": "$",
                        "severity": "trace",
                    }
                ],
            ),
            "invalid field values",
        ),
        (
            lambda result: result.__setitem__("ok", False),
            "does not match its diagnostics",
        ),
    ],
)
def test_native_compiler_plan_rejects_invalid_contracts(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object]], object],
    message: str,
) -> None:
    result = _valid_native_result()
    mutate(result)
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(compile_graph=lambda *args, **kwargs: result),
    )

    with pytest.raises(NativeCompilerContractError, match=message):
        compile_graph_native_plan(
            NORMALIZED_GRAPH,
            block_catalog=BlockCatalog({}),
        )
