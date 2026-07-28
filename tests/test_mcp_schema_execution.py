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
