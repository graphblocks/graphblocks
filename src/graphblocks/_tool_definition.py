"""Single construction path for provider-neutral tool definitions."""

from __future__ import annotations

from collections.abc import Iterable

from .tools import ToolDefinition


def create_tool_definition(
    *,
    name: str,
    description: str,
    input_schema: str,
    output_schema: str | None = None,
    tags: Iterable[str] = (),
    version: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        tags=frozenset(tags),
        version=version,
    )


__all__ = ["create_tool_definition"]
