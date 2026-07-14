from __future__ import annotations

import pytest

from graphblocks.compiler import compile_graph
from graphblocks.plugins import (
    BlockCatalog,
    BlockDescriptor,
    OutputRequirednessPredicate,
    PortDescriptor,
    ResourceSlotDescriptor,
    evaluate_output_requiredness,
    parse_output_requiredness_predicate,
)


def test_direct_block_catalog_rejects_descriptor_identity_mismatch() -> None:
    descriptor = BlockDescriptor("actual.block", 1)

    with pytest.raises(ValueError, match="does not match descriptor 'actual.block@1'"):
        BlockCatalog({"claimed.block@1": descriptor})


@pytest.mark.parametrize(
    "descriptor",
    [
        lambda: PortDescriptor(""),
        lambda: PortDescriptor("bad port"),
        lambda: PortDescriptor("value", type_ref="schemas/Value"),
        lambda: PortDescriptor("value", required="yes"),
        lambda: ResourceSlotDescriptor(""),
        lambda: ResourceSlotDescriptor("model", type_ref="Model"),
        lambda: ResourceSlotDescriptor("model", optional=1),
    ],
)
def test_direct_port_and_resource_slot_descriptors_enforce_invariants(
    descriptor: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        descriptor()  # type: ignore[operator]


@pytest.mark.parametrize("name", ["nested.value", "1value", "value/path"])
def test_direct_port_descriptor_rejects_ambiguous_endpoint_names(name: str) -> None:
    with pytest.raises(ValueError, match="port name must match"):
        PortDescriptor(name)


@pytest.mark.parametrize(
    "descriptor",
    [
        lambda: BlockDescriptor("", 1),
        lambda: BlockDescriptor("bad block", 1),
        lambda: BlockDescriptor("bad.block@1", 1),
        lambda: BlockDescriptor("bad.block", 0),
        lambda: BlockDescriptor("bad.block", True),
        lambda: BlockDescriptor("bad.block", 1, inputs=(object(),)),
        lambda: BlockDescriptor(
            "bad.block",
            1,
            inputs=(PortDescriptor("value"), PortDescriptor("value")),
        ),
        lambda: BlockDescriptor(
            "bad.block",
            1,
            inputs=(
                PortDescriptor(
                    "value",
                    required_when=OutputRequirednessPredicate(
                        operator="phase", phase="resumed"
                    ),
                ),
            ),
        ),
    ],
)
def test_direct_block_descriptors_enforce_catalog_invariants(
    descriptor: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        descriptor()  # type: ignore[operator]


def test_direct_block_descriptors_reject_excessively_nested_config_schemas() -> None:
    config_schema: object = True
    for _ in range(1_100):
        config_schema = {"allOf": [config_schema]}

    with pytest.raises(
        ValueError,
        match="configSchema nesting must not exceed 64 levels",
    ):
        BlockDescriptor(
            "deep.config",
            1,
            config_schema=config_schema,  # type: ignore[arg-type]
        )
    with pytest.raises(
        ValueError,
        match="configSchema nesting must not exceed 64 levels",
    ):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "deep.config",
                    "version": 1,
                    "configSchema": config_schema,
                }
            ]
        )


def test_direct_block_descriptors_accept_config_schema_at_depth_limit() -> None:
    config_schema: object = True
    for _ in range(32):
        config_schema = {"allOf": [config_schema]}

    descriptor = BlockDescriptor(
        "boundary.config",
        1,
        config_schema=config_schema,  # type: ignore[arg-type]
    )
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "boundary.config",
                "version": 1,
                "configSchema": config_schema,
            }
        ]
    )

    assert descriptor.block_id == "boundary.config@1"
    assert catalog.get("boundary.config@1") is not None


def test_direct_block_descriptors_reject_oversized_config_schemas() -> None:
    config_schema = {
        "properties": {f"field_{index}": True for index in range(10_000)}
    }

    with pytest.raises(
        ValueError,
        match="configSchema must not contain more than 10000 JSON nodes",
    ):
        BlockDescriptor("wide.config", 1, config_schema=config_schema)


def test_block_catalog_rejects_invalid_port_schema_ids() -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 output value has invalid type schemas/Text",
    ):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "text.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "schemas/Text"}],
                }
            ]
        )


def test_block_catalog_allows_port_type_expressions() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "control.map",
                "version": 1,
                "inputs": [{"name": "items", "type": "List<Any>"}],
                "outputs": [{"name": "values", "type": "List<Any>"}],
            }
        ]
    )

    assert catalog.get("control.map@1") is not None


@pytest.mark.parametrize("type_ref", ["List<Any", "Tuple<Any>", "Map<String>", 42])
def test_block_catalog_rejects_malformed_type_expressions(type_ref: object) -> None:
    with pytest.raises(ValueError, match="invalid type"):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "bad.block",
                    "version": 1,
                    "outputs": [{"name": "value", "type": type_ref}],
                }
            ]
        )


@pytest.mark.parametrize("field_name", ["required", "optional"])
def test_block_catalog_rejects_non_boolean_contract_flags(field_name: str) -> None:
    block: dict[str, object] = {"typeId": "bad.flags", "version": 1}
    if field_name == "required":
        block["inputs"] = [{"name": "value", "type": "Any", field_name: "false"}]
    else:
        block["resourceSlots"] = [
            {"name": "model", "type": "resources/Model@1", field_name: "false"}
        ]

    with pytest.raises(ValueError, match=f"{field_name} must be a boolean"):
        BlockCatalog.from_blocks([block])  # type: ignore[list-item]


def test_block_catalog_rejects_duplicate_block_contracts() -> None:
    with pytest.raises(ValueError, match="duplicate block catalog descriptor test.echo@1"):
        BlockCatalog.from_blocks(
            [
                {"typeId": "test.echo", "version": 1},
                {"typeId": "test.echo", "version": 1},
            ]
        )


@pytest.mark.parametrize("direction", ["inputs", "outputs"])
def test_block_catalog_rejects_duplicate_port_names(direction: str) -> None:
    with pytest.raises(ValueError, match=f"duplicate {direction[:-1]} 'value'"):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "test.duplicate_ports",
                    "version": 1,
                    direction: [
                        {"name": "value", "type": "graphblocks.ai/Text@1"},
                        {"name": "value", "type": "graphblocks.ai/Text@1"},
                    ],
                }
            ]
        )


def test_block_catalog_descriptors_are_immutable() -> None:
    catalog = BlockCatalog.from_blocks([{"typeId": "test.echo", "version": 1}])

    with pytest.raises(TypeError):
        catalog.descriptors["test.other@1"] = catalog.descriptors["test.echo@1"]  # type: ignore[index]


def test_compile_with_catalog_rejects_undeclared_blocks_by_default() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-block"},
        "spec": {"nodes": {"unknown": {"block": "test.unknown@1"}}},
    }

    plan = compile_graph(graph, block_catalog=BlockCatalog({}))

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1022"]


def test_open_catalog_explicitly_allows_undeclared_blocks() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "dynamic-block"},
        "spec": {"nodes": {"dynamic": {"block": "test.dynamic@1"}}},
    }

    plan = compile_graph(
        graph,
        block_catalog=BlockCatalog({}, allow_unknown_blocks=True),
    )

    assert plan.ok


@pytest.mark.parametrize("version", [True, 0, 1.0, "", "+1", "01", "1.0", "one"])
def test_block_catalog_rejects_non_canonical_block_versions(version: object) -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 version is invalid",
    ):
        BlockCatalog.from_blocks([{"typeId": "bad.version", "version": version}])


def test_block_catalog_rejects_non_canonical_inline_block_version() -> None:
    with pytest.raises(
        ValueError,
        match="block catalog entry 0 version is invalid",
    ):
        BlockCatalog.from_blocks([{"typeId": "bad.version@01"}])


def test_compile_rejects_edge_to_unknown_input_port() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-target-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.missing.field"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1013"]


def test_compile_rejects_edge_from_unknown_output_port() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "bad-source-port"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.missing.field", "to": "sink.text"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1014"]


def test_compile_rejects_required_input_never_produced() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": True}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-required-input"},
        "spec": {"nodes": {"sink": {"block": "text.sink@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1003"]


def test_compile_allows_optional_input_without_edge() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.optional_sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": False}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-input"},
        "spec": {"nodes": {"sink": {"block": "text.optional_sink@1"}}},
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1003" not in [item.code for item in plan.diagnostics.diagnostics]


def test_compile_rejects_optional_output_to_required_input() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "branch.maybe_text",
                "version": 1,
                "outputs": [
                    {"name": "value", "type": "graphblocks.ai/Text@1", "required": False}
                ],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "text", "type": "graphblocks.ai/Text@1", "required": True}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-output-required-input"},
        "spec": {
            "nodes": {
                "maybe": {"block": "branch.maybe_text@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "maybe.value", "to": "sink.text"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1015"]


def test_output_requiredness_predicate_evaluates_config_and_phase_deterministically() -> None:
    predicate = parse_output_requiredness_predicate(
        {
            "all": [
                {"configEquals": {"pointer": "/policy/on~1error", "value": "collect"}},
                {"not": {"phase": "initial"}},
            ]
        }
    )
    config = {"policy": {"on/error": "collect"}}

    assert not evaluate_output_requiredness(predicate, config, phase="initial")
    assert evaluate_output_requiredness(predicate, config, phase="resumed")


@pytest.mark.parametrize(
    "predicate",
    [
        {"operator": "bogus"},
        {"operator": "not"},
        {"operator": "phase", "phase": "finished"},
        {"operator": "configEquals", "pointer": "/value", "expected_json": "{"},
    ],
)
def test_direct_output_requiredness_predicate_construction_fails_closed(
    predicate: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError), match="requiredWhen|not|phase|canonical JSON"):
        OutputRequirednessPredicate(**predicate)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "required_when",
    [
        {},
        {"phase": "finished"},
        {"configEquals": {"pointer": "onError", "value": "collect"}},
        {"configEquals": {"pointer": "/bad~2escape", "value": "collect"}},
        {"configEquals": {"pointer": "/onError", "value": ("collect",)}},
        {"all": []},
        {"all": [{"phase": "initial"}] * 17},
        {"phase": "initial", "not": {"phase": "resumed"}},
    ],
)
def test_block_catalog_rejects_invalid_output_requiredness_predicate(
    required_when: object,
) -> None:
    with pytest.raises(ValueError, match="invalid requiredWhen"):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "branch.conditional",
                    "version": 1,
                    "outputs": [
                        {
                            "name": "value",
                            "required": False,
                            "requiredWhen": required_when,
                        }
                    ],
                }
            ]
        )


def test_block_catalog_rejects_excessive_output_requiredness_nesting() -> None:
    required_when: object = {"phase": "initial"}
    for _ in range(16):
        required_when = {"not": required_when}

    with pytest.raises(ValueError, match="nesting must not exceed 16 levels"):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "branch.too_deep",
                    "version": 1,
                    "outputs": [
                        {
                            "name": "value",
                            "required": False,
                            "requiredWhen": required_when,
                        }
                    ],
                }
            ]
        )


def test_block_catalog_rejects_required_when_on_input() -> None:
    with pytest.raises(ValueError, match="input value must not declare requiredWhen"):
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "branch.invalid_input",
                    "version": 1,
                    "inputs": [
                        {
                            "name": "value",
                            "requiredWhen": {"phase": "resumed"},
                        }
                    ],
                }
            ]
        )


@pytest.mark.parametrize(
    ("on_error", "expected_ok"),
    [("collect", True), ("fail_fast", False)],
)
def test_compile_refines_config_guaranteed_output_requiredness(
    on_error: str,
    expected_ok: bool,
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "branch.map",
                "version": 1,
                "outputs": [
                    {
                        "name": "outcomes",
                        "type": "graphblocks.ai/Outcomes@1",
                        "required": False,
                        "requiredWhen": {
                            "configEquals": {"pointer": "/onError", "value": "collect"}
                        },
                    }
                ],
            },
            {
                "typeId": "outcomes.sink",
                "version": 1,
                "inputs": [
                    {
                        "name": "outcomes",
                        "type": "graphblocks.ai/Outcomes@1",
                    }
                ],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": f"conditional-output-{on_error}"},
        "spec": {
            "nodes": {
                "map": {
                    "block": "branch.map@1",
                    "config": {"onError": on_error},
                },
                "sink": {"block": "outcomes.sink@1"},
            },
            "edges": [{"from": "map.outcomes", "to": "sink.outcomes"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert plan.ok is expected_ok
    error_codes = [
        item.code for item in plan.diagnostics.diagnostics if item.severity == "error"
    ]
    assert error_codes == ([] if expected_ok else ["GB1015"])


def test_compile_keeps_resumed_phase_output_optional_during_initial_compilation() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "async.wait",
                "version": 1,
                "outputs": [
                    {
                        "name": "callback",
                        "required": False,
                        "requiredWhen": {"phase": "resumed"},
                    }
                ],
            },
            {
                "typeId": "callback.sink",
                "version": 1,
                "inputs": [{"name": "callback"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "resumed-output-is-phase-delayed"},
        "spec": {
            "nodes": {
                "wait": {"block": "async.wait@1"},
                "sink": {"block": "callback.sink@1"},
            },
            "edges": [{"from": "wait.callback", "to": "sink.callback"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert [
        item.code for item in plan.diagnostics.diagnostics if item.severity == "error"
    ] == ["GB1015"]


def test_compile_rejects_optional_block_output_to_graph_output() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "branch.maybe_text",
                "version": 1,
                "outputs": [
                    {"name": "value", "type": "graphblocks.ai/Text@1", "required": False}
                ],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "optional-output-graph-output"},
        "spec": {
            "interface": {"outputs": {"value": "graphblocks.ai/Text@1"}},
            "nodes": {"maybe": {"block": "branch.maybe_text@1"}},
            "edges": [{"from": "maybe.value", "to": "$output.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1015"]


def test_compile_rejects_port_type_mismatch() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "number.sink",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Number@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "type-mismatch"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "number.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not plan.ok
    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1018"]


def test_compile_accepts_matching_port_types() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "type-match"},
        "spec": {
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1018" not in [item.code for item in plan.diagnostics.diagnostics]


@pytest.mark.parametrize(
    ("input_schema", "output_schema"),
    [
        ("graphblocks.ai/Number@1", "graphblocks.ai/Text@1"),
        ("graphblocks.ai/Text@1", "graphblocks.ai/Number@1"),
    ],
    ids=["graph-input", "graph-output"],
)
def test_compile_rejects_graph_interface_block_port_type_mismatch(
    input_schema: str,
    output_schema: str,
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.echo",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "interface-type-mismatch"},
        "spec": {
            "interface": {
                "inputs": {"value": input_schema},
                "outputs": {"value": output_schema},
            },
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"},
            ],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == [
        "GB1018"
    ]


def test_compile_accepts_matching_graph_interface_block_port_types() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.echo",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "matching-interface-types"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"},
            },
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"},
            ],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert plan.ok


def test_compile_accepts_dynamic_pseudo_ports_when_graph_interface_is_absent() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.echo",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "dynamic-interface"},
        "spec": {
            "nodes": {"echo": {"block": "text.echo@1"}},
            "edges": [
                {"from": "$input.value", "to": "echo.value"},
                {"from": "echo.value", "to": "$output.value"},
            ],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert plan.ok


@pytest.mark.parametrize(
    ("edge", "expected_code", "expected_message", "expected_path"),
    [
        (
            {"from": "$input.missing.field", "to": "sink.value"},
            "GB1014",
            "graph interface has no input port 'missing'",
            "$.spec.edges[0].from",
        ),
        (
            {"from": "source.value", "to": "$output.missing.field"},
            "GB1013",
            "graph interface has no output port 'missing'",
            "$.spec.edges[0].to",
        ),
    ],
    ids=["input", "output"],
)
@pytest.mark.parametrize("use_catalog", [False, True], ids=["no-catalog", "catalog"])
def test_compile_rejects_unknown_nested_graph_interface_port(
    edge: dict[str, str],
    expected_code: str,
    expected_message: str,
    expected_path: str,
    use_catalog: bool,
) -> None:
    catalog = (
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "text.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                },
                {
                    "typeId": "text.sink",
                    "version": 1,
                    "inputs": [
                        {
                            "name": "value",
                            "type": "graphblocks.ai/Text@1",
                            "required": False,
                        }
                    ],
                },
            ]
        )
        if use_catalog
        else None
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "unknown-nested-interface-port"},
        "spec": {
            "interface": {
                "inputs": {"payload": "graphblocks.ai/Payload@1"},
                "outputs": {"payload": "graphblocks.ai/Payload@1"},
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [edge],
        },
    }

    plan = compile_graph(
        graph,
        block_catalog=catalog,
        allow_unknown_blocks=not use_catalog,
    )

    errors = [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
    assert [(item.code, item.message, item.path) for item in errors] == [
        (expected_code, expected_message, expected_path)
    ]


@pytest.mark.parametrize(
    "edge",
    [
        {"from": "$input.payload.field", "to": "sink.value"},
        {"from": "source.value", "to": "$output.payload.field"},
    ],
    ids=["input", "output"],
)
def test_compile_accepts_declared_nested_graph_interface_port_without_field_type_inference(
    edge: dict[str, str],
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "text.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [
                    {
                        "name": "value",
                        "type": "graphblocks.ai/Text@1",
                        "required": False,
                    }
                ],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "declared-nested-interface-port"},
        "spec": {
            "interface": {
                "inputs": {"payload": "graphblocks.ai/Payload@1"},
                "outputs": {"payload": "graphblocks.ai/Payload@1"},
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [edge],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not [item for item in plan.diagnostics.diagnostics if item.severity == "error"]


@pytest.mark.parametrize(
    ("edge", "expected_message", "expected_path"),
    [
        (
            {"from": "$output.value", "to": "sink.value"},
            "$output cannot be used as an edge source",
            "$.spec.edges[0].from",
        ),
        (
            {"from": "source.value", "to": "$input.value"},
            "$input cannot be used as an edge target",
            "$.spec.edges[0].to",
        ),
    ],
    ids=["output-as-source", "input-as-target"],
)
@pytest.mark.parametrize("use_catalog", [False, True], ids=["no-catalog", "catalog"])
def test_compile_rejects_graph_interface_pseudo_node_in_wrong_direction(
    edge: dict[str, str],
    expected_message: str,
    expected_path: str,
    use_catalog: bool,
) -> None:
    catalog = (
        BlockCatalog.from_blocks(
            [
                {
                    "typeId": "text.source",
                    "version": 1,
                    "outputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
                },
                {
                    "typeId": "text.sink",
                    "version": 1,
                    "inputs": [
                        {
                            "name": "value",
                            "type": "graphblocks.ai/Text@1",
                            "required": False,
                        }
                    ],
                },
            ]
        )
        if use_catalog
        else None
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "wrong-pseudo-direction"},
        "spec": {
            "interface": {
                "inputs": {"value": "graphblocks.ai/Text@1"},
                "outputs": {"value": "graphblocks.ai/Text@1"},
            },
            "nodes": {
                "source": {"block": "text.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [edge],
        },
    }

    plan = compile_graph(
        graph,
        block_catalog=catalog,
        allow_unknown_blocks=not use_catalog,
    )

    errors = [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
    assert [(item.code, item.message, item.path) for item in errors] == [
        ("GB1020", expected_message, expected_path)
    ]


def test_compile_preserves_any_wildcard_for_node_to_node_ports() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "any.source",
                "version": 1,
                "outputs": [{"name": "value", "type": "Any"}],
            },
            {
                "typeId": "text.sink",
                "version": 1,
                "inputs": [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "node-any-wildcard"},
        "spec": {
            "nodes": {
                "source": {"block": "any.source@1"},
                "sink": {"block": "text.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert "GB1018" not in [item.code for item in plan.diagnostics.diagnostics]


@pytest.mark.parametrize(
    ("source_outputs", "sink_inputs", "expected_code"),
    [
        (
            [],
            [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            "GB1014",
        ),
        (
            [{"name": "value", "type": "graphblocks.ai/Text@1"}],
            [],
            "GB1013",
        ),
    ],
    ids=["empty-outputs", "empty-inputs"],
)
def test_compile_rejects_edge_against_empty_descriptor_port_direction(
    source_outputs: list[dict[str, str]],
    sink_inputs: list[dict[str, str]],
    expected_code: str,
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {"typeId": "test.source", "version": 1, "outputs": source_outputs},
            {"typeId": "test.sink", "version": 1, "inputs": sink_inputs},
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "empty-descriptor-port-direction"},
        "spec": {
            "nodes": {
                "source": {"block": "test.source@1"},
                "sink": {"block": "test.sink@1"},
            },
            "edges": [{"from": "source.value", "to": "sink.value"}],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == [
        expected_code
    ]


@pytest.mark.parametrize(
    "edge",
    [
        {"from": "source.payload.field", "to": "sink.value"},
        {"from": "source.value", "to": "sink.payload.field"},
    ],
    ids=["nested-source", "nested-target"],
)
def test_compile_checks_nested_node_parent_port_without_inferring_field_type(
    edge: dict[str, str],
) -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "test.source",
                "version": 1,
                "outputs": [
                    {"name": "payload", "type": "graphblocks.ai/Payload@1"},
                    {"name": "value", "type": "graphblocks.ai/Text@1"},
                ],
            },
            {
                "typeId": "test.sink",
                "version": 1,
                "inputs": [
                    {
                        "name": "payload",
                        "type": "graphblocks.ai/Payload@1",
                        "required": False,
                    },
                    {
                        "name": "value",
                        "type": "graphblocks.ai/Text@1",
                        "required": False,
                    },
                ],
            },
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "nested-node-port"},
        "spec": {
            "nodes": {
                "source": {"block": "test.source@1"},
                "sink": {"block": "test.sink@1"},
            },
            "edges": [edge],
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
