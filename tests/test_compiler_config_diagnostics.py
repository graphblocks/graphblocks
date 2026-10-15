from __future__ import annotations

from collections.abc import Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
import pytest

from graphblocks.compiler import (
    MAX_CONFIG_VALIDATION_ERROR_MESSAGE_CHARS,
    Plan,
    _config_error_message,
    compile_graph,
    compile_graph_reference,
)
from graphblocks.plugins import BlockCatalog


@pytest.mark.parametrize(
    "keyword",
    [
        "type",
        "required",
        "additionalProperties",
        "const",
        "enum",
        "uniqueItems",
        "oneOf",
        "anyOf",
        "not",
        "pattern",
    ],
)
def test_config_error_message_bounds_every_rendering_branch(keyword: str) -> None:
    large = "😀" * 5_000
    error = ValidationError(
        large,
        validator=keyword,
        validator_value=[large] if keyword in {"required", "enum"} else large,
        instance={large: True} if keyword == "additionalProperties" else {},
        schema={"additionalProperties": False},
    )

    message = _config_error_message(error)

    assert len(message) <= MAX_CONFIG_VALIDATION_ERROR_MESSAGE_CHARS
    assert len(message.encode("utf-8")) <= 4 * MAX_CONFIG_VALIDATION_ERROR_MESSAGE_CHARS
    if keyword in {"type", "required", "additionalProperties", "const", "enum", "pattern"}:
        assert message.endswith("...")


@pytest.mark.parametrize("compiler", [compile_graph_reference, compile_graph])
@pytest.mark.parametrize("keyword", ["const", "enum", "required", "additionalProperties"])
def test_compile_bounds_normalized_config_diagnostics(
    compiler: Callable[..., Plan], keyword: str
) -> None:
    large = "😀" * 5_000
    schema = {
        "const": {"const": large},
        "enum": {"enum": [large]},
        "required": {"type": "object", "required": [large]},
        "additionalProperties": {
            "type": "object", "properties": {"allowed": {}}, "additionalProperties": False
        },
    }[keyword]
    config = {large: True} if keyword == "additionalProperties" else {}
    catalog = BlockCatalog.from_blocks(
        [{"typeId": "test.bounded", "version": 1, "configSchema": schema}]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "bounded-config-diagnostic"},
        "spec": {"nodes": {"node": {"block": "test.bounded@1", "config": config}}},
    }

    errors = [
        diagnostic
        for diagnostic in compiler(graph, block_catalog=catalog).diagnostics.diagnostics
        if diagnostic.code == "GB2019"
    ]

    assert len(errors) == 1
    message = errors[0].message.removeprefix(
        "node config does not satisfy test.bounded@1 configSchema: "
    )
    assert len(message) == MAX_CONFIG_VALIDATION_ERROR_MESSAGE_CHARS
    assert len(message.encode("utf-8")) <= 4 * MAX_CONFIG_VALIDATION_ERROR_MESSAGE_CHARS
    assert message.endswith("...")


@pytest.mark.parametrize("compiler", [compile_graph_reference, compile_graph])
@pytest.mark.parametrize("pattern", ["^x-", "allowed$"])
def test_config_diagnostics_preserve_pattern_properties(
    compiler: Callable[..., Plan], pattern: str
) -> None:
    schema = {
        "type": "object",
        "properties": {"explicit": {}},
        "patternProperties": {pattern: {"type": "string"}},
        "additionalProperties": False,
    }
    config = {"explicit": 1, "bad": 2, "x-allowed": "ok"}
    raw_errors = list(Draft202012Validator(schema).iter_errors(config))
    assert len(raw_errors) == 1
    assert _config_error_message(raw_errors[0]) == (
        'unexpected properties are not allowed: ["bad"]'
    )
    catalog = BlockCatalog.from_blocks(
        [{"typeId": "test.patterns", "version": 1, "configSchema": schema}]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "pattern-properties-diagnostic"},
        "spec": {"nodes": {"node": {"block": "test.patterns@1", "config": config}}},
    }

    errors = [
        diagnostic
        for diagnostic in compiler(graph, block_catalog=catalog).diagnostics.diagnostics
        if diagnostic.code == "GB2019"
    ]

    assert len(errors) == 1
    assert errors[0].message.endswith(
        'unexpected properties are not allowed: ["bad"]'
    )
