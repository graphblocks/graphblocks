from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from graphblocks._schema_execution import (
    DEFAULT_SCHEMA_EXECUTION_POLICY,
    SchemaExecutionPolicy,
    SchemaExecutionPolicyError,
    enforce_schema_execution_policy,
    find_regular_expression_keyword,
)
from graphblocks.integrations.mcp import McpInlineSchemaRegistry
from graphblocks.integrations.openai import _validated_inline_json_schema
from graphblocks.plugins import BlockDescriptor


SchemaEntryPoint = Callable[[Mapping[str, object]], object]


class _UnstableMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):
        return iter(("value",))

    def __len__(self) -> int:
        return 1

    def values(self):
        raise RuntimeError("mapping changed")


class _UnstableSequence(Sequence[object]):
    def __getitem__(self, index: int) -> object:
        raise RuntimeError(f"sequence changed at {index}")

    def __len__(self) -> int:
        return 1


def _mcp_schema_entry(schema: Mapping[str, object]) -> object:
    return McpInlineSchemaRegistry({"schemas/External@1": schema})


def _plugin_schema_entry(schema: Mapping[str, object]) -> object:
    return BlockDescriptor("external.block", 1, config_schema=schema)


def _openai_schema_entry(schema: Mapping[str, object]) -> object:
    return _validated_inline_json_schema("schemas/External@1", schema)


SCHEMA_ENTRY_POINTS: tuple[tuple[str, SchemaEntryPoint], ...] = (
    ("mcp-inline-schema", _mcp_schema_entry),
    ("plugin-config-schema", _plugin_schema_entry),
    ("openai-tool-schema", _openai_schema_entry),
)


def test_external_schema_entry_point_inventory_is_closed() -> None:
    assert tuple(name for name, _entry in SCHEMA_ENTRY_POINTS) == (
        "mcp-inline-schema",
        "plugin-config-schema",
        "openai-tool-schema",
    )


def _oversized_node_schema() -> dict[str, object]:
    return {
        "properties": {
            f"field_{index}": True
            for index in range(DEFAULT_SCHEMA_EXECUTION_POLICY.max_nodes)
        }
    }


@pytest.mark.parametrize(
    ("case_name", "schema_factory"),
    (
        ("remote-ref", lambda: {"$ref": "https://attacker.invalid/schema"}),
        ("redos-pattern", lambda: {"type": "string", "pattern": "^(a+)+$"}),
        ("node-budget", _oversized_node_schema),
    ),
)
@pytest.mark.parametrize(("entry_name", "entry"), SCHEMA_ENTRY_POINTS)
def test_external_schema_entry_points_reject_the_common_malicious_corpus(
    entry_name: str,
    entry: SchemaEntryPoint,
    case_name: str,
    schema_factory: Callable[[], Mapping[str, object]],
) -> None:
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        entry(schema_factory())


def test_schema_execution_policy_reports_closed_resource_metrics() -> None:
    metrics = enforce_schema_execution_policy(
        {
            "$defs": {"value": {"type": "string", "pattern": "^[a-z]+$"}},
            "$ref": "#/$defs/value",
        }
    )

    assert metrics.schema_bytes > 0
    assert metrics.nodes == 6
    assert metrics.depth == 3
    assert metrics.validation_steps == 2


@pytest.mark.parametrize(
    ("policy", "schema", "code"),
    (
        (
            SchemaExecutionPolicy(max_schema_bytes=8),
            {"type": "object"},
            "max_schema_bytes",
        ),
        (
            SchemaExecutionPolicy(max_nodes=2),
            {"allOf": [True]},
            "max_nodes",
        ),
        (
            SchemaExecutionPolicy(max_depth=1),
            {"allOf": [{"type": "object"}]},
            "max_depth",
        ),
        (
            SchemaExecutionPolicy(max_pattern_bytes=4),
            {"type": "string", "pattern": "^[a-z]+$"},
            "max_pattern_bytes",
        ),
        (
            SchemaExecutionPolicy(max_validation_steps=1),
            {"allOf": [{"type": "object"}, {"type": "string"}]},
            "max_validation_steps",
        ),
    ),
)
def test_schema_execution_policy_enforces_each_resource_ceiling(
    policy: SchemaExecutionPolicy,
    schema: Mapping[str, object],
    code: str,
) -> None:
    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy(schema, policy=policy)

    assert captured.value.code == code


def test_schema_execution_policy_allows_safe_patterns_but_rejects_backtracking() -> (
    None
):
    enforce_schema_execution_policy({"type": "string", "pattern": r"^\S+@[1-9][0-9]*$"})

    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy({"type": "string", "pattern": "^(a|aa)+$"})

    assert captured.value.code == "unsafe_pattern"


def test_schema_execution_policy_does_not_treat_instance_annotations_as_schemas() -> (
    None
):
    enforce_schema_execution_policy(
        {
            "type": "object",
            "examples": [
                {
                    "pattern": "^(a+)+$",
                    "$ref": "https://example.invalid/instance-field",
                }
            ],
        }
    )


def test_schema_execution_policy_rejects_recursive_values_before_serialization() -> (
    None
):
    recursive: dict[str, object] = {}
    recursive["allOf"] = [recursive]

    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy(recursive)

    assert captured.value.code == "recursive_schema"


def test_schema_execution_policy_rejects_recursive_sequences() -> None:
    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy({"allOf": recursive})

    assert captured.value.code == "recursive_schema"


@pytest.mark.parametrize(
    "schema",
    (_UnstableMapping(), {"allOf": _UnstableSequence()}),
)
def test_schema_execution_policy_rejects_unstable_containers(
    schema: Mapping[str, object],
) -> None:
    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy(schema)

    assert captured.value.code == "unstable_schema"


@pytest.mark.parametrize("pattern", (r"^(a)\1$", r"^(?=a)a$"))
def test_schema_execution_policy_rejects_advanced_backtracking_patterns(
    pattern: str,
) -> None:
    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy({"type": "string", "pattern": pattern})

    assert captured.value.code == "unsafe_pattern"


def test_schema_execution_policy_checks_pattern_properties() -> None:
    with pytest.raises(SchemaExecutionPolicyError) as captured:
        enforce_schema_execution_policy(
            {"type": "object", "patternProperties": {"(a+)+": True}}
        )

    assert captured.value.code == "unsafe_pattern"
    assert (
        find_regular_expression_keyword(
            {"type": "object", "patternProperties": {"^safe$": True}}
        )
        == "patternProperties"
    )
    assert find_regular_expression_keyword({"pattern": "^safe$"}) == "pattern"


def test_schema_execution_policy_never_resolves_remote_references() -> None:
    metrics = enforce_schema_execution_policy(
        {"$ref": "https://schemas.invalid/external"},
        policy=SchemaExecutionPolicy(allow_remote_ref=True),
    )
    unresolved_local = enforce_schema_execution_policy({"$ref": "#/$defs/missing"})

    assert metrics.validation_steps == 1
    assert unresolved_local.validation_steps == 1


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    (
        ("max_nodes", True, ValueError),
        ("max_depth", 0, ValueError),
        ("max_schema_bytes", 1.5, ValueError),
        ("allow_pattern", 1, TypeError),
        ("allow_remote_ref", "yes", TypeError),
    ),
)
def test_schema_execution_policy_rejects_invalid_policy_values(
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        SchemaExecutionPolicy(**{field_name: value})  # type: ignore[arg-type]


def test_schema_execution_policy_validates_its_api_boundary() -> None:
    assert enforce_schema_execution_policy(True).validation_steps == 0
    with pytest.raises(TypeError, match="schema"):
        enforce_schema_execution_policy([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="policy"):
        enforce_schema_execution_policy(  # type: ignore[arg-type]
            {},
            policy=object(),
        )
    with pytest.raises(ValueError, match="owner"):
        enforce_schema_execution_policy({}, owner="")
