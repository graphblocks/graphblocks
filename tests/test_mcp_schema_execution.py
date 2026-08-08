from __future__ import annotations

import importlib

import pytest


def test_mcp_inline_schemas_reject_regular_expression_keywords() -> None:
    graphblocks_mcp = importlib.import_module("graphblocks.integrations.mcp")

    unsafe_schemas = (
        {
            "type": "string",
            "pattern": "^(a+)+$",
        },
        {
            "type": "object",
            "patternProperties": {
                "^(a+)+$": {"type": "string"},
            },
        },
        {
            "allOf": [
                {
                    "type": "object",
                    "propertyNames": {"pattern": "^(a+)+$"},
                }
            ]
        },
        {
            "examples": [{"pattern": "^(a+)+$"}],
            "$ref": "#/examples/0",
        },
    )

    for schema in unsafe_schemas:
        with pytest.raises(
            graphblocks_mcp.McpToolAdapterError,
            match="regular-expression keyword.*disabled",
        ):
            graphblocks_mcp.McpInlineSchemaRegistry(
                {"schemas/Untrusted@1": schema}
            )


def test_mcp_inline_schema_allows_pattern_named_instance_fields() -> None:
    graphblocks_mcp = importlib.import_module("graphblocks.integrations.mcp")
    registry = graphblocks_mcp.McpInlineSchemaRegistry(
        {
            "schemas/PatternField@1": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                },
                "examples": [{"pattern": "^(a+)+$"}],
                "required": ["pattern"],
                "additionalProperties": False,
            }
        }
    )

    registry.validate("schemas/PatternField@1", {"pattern": "literal value"})


def test_mcp_inline_schema_registry_reuses_compiled_validator(monkeypatch) -> None:
    graphblocks_mcp = importlib.import_module("graphblocks.integrations.mcp")
    schema_id = "schemas/Cached@1"
    registry = graphblocks_mcp.McpInlineSchemaRegistry(
        {
            schema_id: {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        }
    )

    def unexpected_validator_construction(schema: object) -> None:
        del schema
        raise AssertionError("validator was reconstructed during validation")

    monkeypatch.setattr(
        graphblocks_mcp,
        "Draft202012Validator",
        unexpected_validator_construction,
    )

    for _ in range(10_000):
        registry.validate(schema_id, {"value": "cached"})


def test_mcp_discovery_parses_schema_once_and_returns_detached_projections(
    monkeypatch,
) -> None:
    graphblocks_mcp = importlib.import_module("graphblocks.integrations.mcp")
    schema_id = "schemas/Discovered@1"
    discovery = graphblocks_mcp.McpToolDiscovery(
        (),
        (
            (
                schema_id,
                '{"properties":{"value":{"type":"string"}},"type":"object"}',
            ),
        ),
    )

    def unexpected_parse(document: str) -> object:
        del document
        raise AssertionError("schema document was reparsed during projection")

    monkeypatch.setattr(graphblocks_mcp, "canonical_loads", unexpected_parse)

    first = discovery.schemas
    first[schema_id]["properties"] = {"mutated": True}
    for _ in range(10_000):
        projection = discovery.schemas
        assert projection[schema_id] == {
            "properties": {"value": {"type": "string"}},
            "type": "object",
        }
