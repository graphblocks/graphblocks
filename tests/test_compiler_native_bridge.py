from __future__ import annotations

from collections.abc import Callable
import sys
from types import SimpleNamespace

import pytest

from graphblocks.canonical import canonical_hash
from graphblocks.compiler import (
    NativeCompilerContractError,
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
    compatibility_plan = compile_graph(
        NORMALIZED_GRAPH,
        block_catalog=catalog,
    )

    assert reference_plan.to_dict() == compatibility_plan.to_dict()


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

    plan = compile_graph_native_plan(NORMALIZED_GRAPH, block_catalog=[])

    assert isinstance(plan, Plan)
    assert plan.to_dict() == result


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
        (item.code, item.severity, item.path)
        for item in native.diagnostics.diagnostics
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
        (item.code, item.severity, item.path)
        for item in native.diagnostics.diagnostics
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
        (item.code, item.severity, item.path)
        for item in native.diagnostics.diagnostics
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
        compile_graph_native_plan(NORMALIZED_GRAPH, block_catalog=[])
