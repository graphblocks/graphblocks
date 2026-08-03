from __future__ import annotations
from time import perf_counter
from graphblocks.integrations.mcp import McpInlineSchemaRegistry
from graphblocks import ToolSchemaValidationError

registry = McpInlineSchemaRegistry({
    "evil": {"type": "string", "pattern": "^(a+)+$"},
})
for size in (18, 20, 22, 24, 26):
    value = "a" * size + "!"
    started = perf_counter()
    try:
        registry.validate("evil", value)
    except ToolSchemaValidationError:
        pass
    elapsed = perf_counter() - started
    print(size, f"{elapsed:.6f}")
