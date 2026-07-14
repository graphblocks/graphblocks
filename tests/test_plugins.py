from __future__ import annotations

import pytest
import yaml

from graphblocks import BlockCatalog, compile_graph, discover_plugins, load_plugin_manifest, validate_plugin_manifest
from graphblocks.cli import main
from graphblocks.plugins import builtin_block_catalog, plugin_manifest_from_document
from graphblocks.runtime import stdlib_registry


def test_builtin_plugin_discovery_does_not_need_installed_scan() -> None:
    registry = discover_plugins(include_installed=False)

    assert registry.ok
    assert [manifest.plugin_id for manifest in registry.manifests] == ["io.graphblocks.stdlib"]



def test_alpha_plugin_manifest_is_migrated_to_auditable_v1() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "example.alpha"},
        "spec": {
            "pluginId": "example.alpha",
            "version": "1.0.0",
            "blocks": [],
        },
    }

    assert validate_plugin_manifest(document).ok
    manifest = plugin_manifest_from_document(document)

    assert manifest.raw["apiVersion"] == "graphblocks.ai/v1"
    assert (
        manifest.raw["metadata"]["annotations"]["graphblocks.ai/migratedFrom"]
        == "graphblocks.ai/v1alpha1"
    )


def test_alpha_plugin_manifest_migration_completes_block_contract_defaults() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "example.alpha_block"},
        "spec": {
            "pluginId": "example.alpha_block",
            "version": "1.0.0",
            "blocks": [{"typeId": "example.echo", "version": 1}],
        },
    }

    manifest = plugin_manifest_from_document(document)

    assert manifest.blocks[0]["capabilities"] == []
    assert manifest.blocks[0]["configSchema"] == {"type": "object"}
    assert "capabilities" not in document["spec"]["blocks"][0]
    assert "configSchema" not in document["spec"]["blocks"][0]


def test_stable_plugin_blocks_require_capabilities_and_config_schema() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "PluginManifest",
            "metadata": {"name": "example.incomplete"},
            "spec": {
                "pluginId": "example.incomplete",
                "version": "1.0.0",
                "blocks": [{"typeId": "example.echo", "version": 1}],
            },
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["GB2018", "GB2018"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].capabilities",
        "$.spec.blocks[0].configSchema",
    ]


def test_plugin_manifest_reports_recursive_required_when_as_a_diagnostic() -> None:
    required_when: dict[str, object] = {}
    required_when["not"] = required_when
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "example.recursive"},
        "spec": {
            "pluginId": "example.recursive",
            "version": "1.0.0",
            "capabilities": [],
            "blocks": [
                {
                    "typeId": "example.echo",
                    "version": 1,
                    "capabilities": [],
                    "configSchema": {"type": "object"},
                    "outputs": [
                        {
                            "name": "value",
                            "required": False,
                            "requiredWhen": required_when,
                        }
                    ],
                }
            ],
        },
    }

    diagnostics = validate_plugin_manifest(document)

    assert [(item.code, item.path) for item in diagnostics.diagnostics] == [
        ("GB0014", "$.spec.blocks[0].outputs[0].requiredWhen.not")
    ]


def test_stable_plugin_config_schema_is_meta_validated_as_draft_2020_12() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "PluginManifest",
            "metadata": {"name": "example.invalid_schema"},
            "spec": {
                "pluginId": "example.invalid_schema",
                "version": "1.0.0",
                "blocks": [
                    {
                        "typeId": "example.echo",
                        "version": 1,
                        "capabilities": ["example.echo"],
                        "configSchema": {"type": "not-a-json-type"},
                    }
                ],
            },
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["GB2018"]
    assert diagnostics.diagnostics[0].path == "$.spec.blocks[0].configSchema"
    assert "Draft 2020-12" in diagnostics.diagnostics[0].message


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_stable_plugin_config_schema_rejects_external_references(
    keyword: str,
) -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1",
            "kind": "PluginManifest",
            "metadata": {"name": "example.external_schema"},
            "spec": {
                "pluginId": "example.external_schema",
                "version": "1.0.0",
                "blocks": [
                    {
                        "typeId": "example.echo",
                        "version": 1,
                        "capabilities": [],
                        "configSchema": {keyword: "file:///etc/passwd"},
                    }
                ],
            },
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["GB2018"]
    assert diagnostics.diagnostics[0].path == "$.spec.blocks[0].configSchema"
    assert f"{keyword} references must be local fragments" in diagnostics.diagnostics[0].message


def test_closed_v1_plugin_schema_is_enforced_by_direct_and_cli_readers(
    tmp_path,
    capsys,
) -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "PluginManifest",
        "metadata": {"name": "example.closed"},
        "spec": {
            "pluginId": "example.closed",
            "version": "1.0.0",
            "blocks": [],
            "mystery": True,
        },
    }
    path = tmp_path / "plugin.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    diagnostics = validate_plugin_manifest(document)
    assert any(
        item.code == "GB0014" and item.path == "$.spec"
        for item in diagnostics.diagnostics
    )
    with pytest.raises(ValueError, match="GB0014"):
        load_plugin_manifest(path)
    with pytest.raises(ValueError, match="GB0014"):
        plugin_manifest_from_document(document)
    assert main(["plugins", "validate", str(path), "--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out
def test_plugin_manifest_validation_requires_plugin_id() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {},
            "spec": {"blocks": []},
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2006"]


@pytest.mark.parametrize("version", [True, 0, 1.0, "", "+1", "01", "1.0", "one"])
def test_plugin_manifest_validation_rejects_non_canonical_block_versions(version: object) -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_version"},
            "spec": {
                "pluginId": "com.example.bad_version",
                "blocks": [{"typeId": "bad.version", "version": version}],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2016"]
    assert [item.path for item in diagnostics.diagnostics] == ["$.spec.blocks[0].version"]


def test_plugin_manifest_validation_rejects_non_canonical_inline_block_version() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_inline_version"},
            "spec": {
                "pluginId": "com.example.bad_inline_version",
                "blocks": [{"typeId": "bad.version@01"}],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2016"]
    assert [item.path for item in diagnostics.diagnostics] == ["$.spec.blocks[0].typeId"]


def test_plugin_manifest_rejects_duplicate_block_id_with_different_implementations() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.duplicate_blocks"},
            "spec": {
                "pluginId": "com.example.duplicate_blocks",
                "blocks": [
                    {
                        "typeId": "test.echo",
                        "version": 1,
                        "implementation": "one.echo",
                    },
                    {
                        "typeId": "test.echo",
                        "version": 1,
                        "implementation": "two.echo",
                    },
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2011"]


def test_plugin_manifest_validation_rejects_duplicate_port_names() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.duplicate_ports"},
            "spec": {
                "pluginId": "com.example.duplicate_ports",
                "blocks": [
                    {
                        "typeId": "test.echo",
                        "version": 1,
                        "outputs": [
                            {"name": "value", "type": "graphblocks.ai/Text@1"},
                            {"name": "value", "type": "graphblocks.ai/Text@1"},
                        ],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2015"]


def test_plugin_manifest_validates_output_requiredness_predicates() -> None:
    document = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "com.example.conditional_outputs"},
        "spec": {
            "pluginId": "com.example.conditional_outputs",
            "blocks": [
                {
                    "typeId": "branch.conditional",
                    "version": 1,
                    "outputs": [
                        {
                            "name": "value",
                            "required": False,
                            "requiredWhen": {
                                "any": [
                                    {
                                        "configEquals": {
                                            "pointer": "/onError",
                                            "value": "collect",
                                        }
                                    },
                                    {"phase": "resumed"},
                                ]
                            },
                        }
                    ],
                }
            ],
        },
    }

    assert validate_plugin_manifest(document).ok

    document["spec"]["blocks"][0]["outputs"][0]["requiredWhen"] = {  # type: ignore[index]
        "phase": "finished"
    }
    diagnostics = validate_plugin_manifest(document)
    assert [item.code for item in diagnostics.diagnostics] == ["GB2015"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].outputs[0].requiredWhen"
    ]


def test_plugin_manifest_rejects_required_when_on_input() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.invalid_input_predicate"},
            "spec": {
                "pluginId": "com.example.invalid_input_predicate",
                "blocks": [
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
                ],
            },
        }
    )

    assert [item.code for item in diagnostics.diagnostics] == ["GB2015"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].inputs[0].requiredWhen"
    ]


def test_duplicate_plugin_id_is_registry_error(tmp_path) -> None:
    manifest = {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"name": "com.example.duplicate", "version": "1.0.0"},
        "spec": {"pluginId": "com.example.duplicate", "blocks": []},
    }
    (tmp_path / "one.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (tmp_path / "two.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    registry = discover_plugins([tmp_path], include_installed=False)

    assert not registry.ok
    assert any(item.code == "GB2013" for item in registry.diagnostics.diagnostics)


def test_builtin_plugin_exposes_stdlib_port_descriptors() -> None:
    registry = discover_plugins(include_installed=False)
    catalog = BlockCatalog.from_manifests(registry.manifests)

    prompt = catalog.get("prompt.render@1")
    model = catalog.get("model.generate@1")
    resolve_tools = catalog.get("tools.resolve@1")
    agent = catalog.get("agent.run@1")
    control_map = catalog.get("control.map@2")
    control_select = catalog.get("control.select@1")
    structured = catalog.get("model.structured_generate@1")
    fuse = catalog.get("retrieve.fuse@1")
    retrieve = catalog.get("retrieve.execute_plan@1")

    assert prompt is not None
    assert [port.name for port in prompt.inputs] == ["message"]
    assert [port.name for port in prompt.outputs] == ["prompt"]
    assert model is not None
    assert [port.name for port in model.inputs] == ["prompt", "context"]
    assert [port.name for port in model.outputs] == ["response"]
    assert resolve_tools is not None
    assert [port.name for port in resolve_tools.inputs] == ["principal", "conversation", "policySnapshot"]
    assert [port.name for port in resolve_tools.outputs] == ["tools"]
    assert agent is not None
    assert [port.name for port in agent.inputs] == [
        "messages",
        "tools",
        "context",
        "objective",
        "diagnostics",
        "conversation",
    ]
    assert [port.name for port in agent.outputs] == ["candidate", "result", "message"]
    assert control_map is not None
    assert [port.name for port in control_map.inputs] == ["items"]
    assert [port.name for port in control_map.outputs] == ["values", "outcomes"]
    outcomes = next(port for port in control_map.outputs if port.name == "outcomes")
    assert outcomes.required_for({"onError": "collect"}, phase="initial")
    assert not outcomes.required_for({"onError": "fail_fast"}, phase="initial")
    await_callback = catalog.get("async.await_callback@1")
    assert await_callback is not None
    for output_name in ("callback", "operation"):
        output = next(port for port in await_callback.outputs if port.name == output_name)
        assert not output.required_for({}, phase="initial")
        assert output.required_for({}, phase="resumed")
    assert control_select is not None
    assert control_select.inputs[0].type_ref == "graphblocks.ai/Cases@1"
    assert structured is not None
    assert {port.name: port.type_ref for port in structured.outputs}["items"] == "graphblocks.ai/StructuredItems@1"
    assert {port.name: port.type_ref for port in structured.outputs}["schemaId"] == "graphblocks.ai/String@1"
    assert fuse is not None
    assert fuse.outputs[0].type_ref == "graphblocks.ai/SearchHits@1"
    assert retrieve is not None
    assert [port.name for port in retrieve.outputs] == ["result", "sources"]


def test_builtin_plugin_blocks_declare_and_preserve_stable_contract_fields() -> None:
    registry = discover_plugins(include_installed=False)
    manifest = registry.manifests[0]
    preview_catalog = BlockCatalog.from_manifests(registry.manifests)
    stable_catalog = builtin_block_catalog(profile="stable")

    assert manifest.blocks
    assert all("capabilities" in block for block in manifest.blocks)
    assert all("configSchema" in block for block in manifest.blocks)
    stable_blocks = {
        "control.map@2",
        "control.select@1",
        "model.generate@1",
        "prompt.render@1",
    }
    assert all(
        descriptor.config_schema.get("additionalProperties") is False
        for block_id, descriptor in preview_catalog.descriptors.items()
        if block_id in stable_blocks
    )
    assert set(stable_catalog.descriptors) == stable_blocks
    prompt = stable_catalog.get("prompt.render@1")
    control_map = stable_catalog.get("control.map@2")
    preview_control_map = preview_catalog.get("control.map@2")
    assert prompt is not None
    assert control_map is not None
    assert preview_control_map is not None
    assert prompt.capabilities == ()
    assert dict(prompt.config_schema) == {
        "additionalProperties": False,
        "properties": {"template": {"type": "string"}},
        "type": "object",
    }
    assert control_map.config_schema["required"] == ("block",)
    assert set(control_map.config_schema["properties"]) == {
        "block",
        "config",
        "inputName",
        "onError",
        "outputName",
    }
    assert "graph" in preview_control_map.config_schema["properties"]


def test_compiler_validates_node_config_against_resolved_block_schema() -> None:
    catalog = BlockCatalog.from_blocks(
        [
            {
                "typeId": "example.configured",
                "version": 1,
                "capabilities": ["example.configured"],
                "configSchema": {
                    "type": "object",
                    "properties": {"threshold": {"type": "integer"}},
                    "required": ["threshold"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "invalid-node-config"},
        "spec": {
            "nodes": {
                "configured": {
                    "block": "example.configured@1",
                    "config": {"threshold": "high"},
                }
            }
        },
    }

    first = compile_graph(graph, block_catalog=catalog)
    second = compile_graph(graph, block_catalog=catalog)
    config_diagnostics = [
        item for item in first.diagnostics.diagnostics if item.code == "GB2019"
    ]

    assert first.diagnostics.to_list() == second.diagnostics.to_list()
    assert [item.path for item in config_diagnostics] == [
        "$.spec.nodes.configured.config.threshold"
    ]
    assert "JSON type \"integer\"" in config_diagnostics[0].message


def test_builtin_catalog_covers_every_python_stdlib_runtime_block() -> None:
    registry = discover_plugins(include_installed=False)
    catalog = BlockCatalog.from_manifests(registry.manifests)

    assert set(catalog.descriptors) == set(stdlib_registry().blocks)


def test_builtin_retrieval_descriptor_compiles_federated_source_wiring() -> None:
    registry = discover_plugins(include_installed=False)
    catalog = BlockCatalog.from_manifests(registry.manifests)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "typed-retrieval-contract"},
        "spec": {
            "nodes": {
                "retrieve": {
                    "block": "retrieve.execute_plan@1",
                    "inputs": {
                        "query": "$input.query",
                        "sources": "$input.sources",
                    },
                },
                "fuse": {
                    "block": "retrieve.fuse@1",
                    "inputs": {"sources": "retrieve.sources"},
                },
            }
        },
    }

    plan = compile_graph(graph, block_catalog=catalog)

    assert not [
        diagnostic
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ]


def test_builtin_plugin_describes_all_documented_portable_blocks() -> None:
    registry = discover_plugins(include_installed=False)
    catalog = BlockCatalog.from_manifests(registry.manifests)

    expected = {
        "model.structured_generate@1",
        "retrieve.execute_plan@1",
        "retrieve.fuse@1",
        "rank.documents@1",
        "context.build@1",
        "answer.validate_grounding@1",
        "check.run_suite@1",
        "gate.evaluate@1",
        "review.request@1",
        "result.bundle@1",
    }

    assert expected <= set(catalog.descriptors)


def test_plugin_manifest_validation_rejects_port_without_name() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_ports"},
            "spec": {
                "pluginId": "com.example.bad_ports",
                "blocks": [
                    {
                        "typeId": "bad.block",
                        "version": 1,
                        "inputs": [{"type": "graphblocks.ai/Text@1"}],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2015"]


def test_plugin_manifest_validation_rejects_invalid_descriptor_schema_ids() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_schema_refs"},
            "spec": {
                "pluginId": "com.example.bad_schema_refs",
                "blocks": [
                    {
                        "typeId": "bad.block",
                        "version": 1,
                        "inputs": [{"name": "message", "type": "schemas/Message"}],
                        "resourceSlots": [
                            {"name": "store", "type": "resources/VectorStore"},
                        ],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB0015", "GB0015"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].inputs[0].type",
        "$.spec.blocks[0].resourceSlots[0].type",
    ]


def test_plugin_manifest_validation_allows_descriptor_type_expressions() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.type_expressions"},
            "spec": {
                "pluginId": "com.example.type_expressions",
                "blocks": [
                    {
                        "typeId": "control.map",
                        "version": 1,
                        "inputs": [{"name": "items", "type": "List<Any>"}],
                        "outputs": [{"name": "values", "type": "List<Any>"}],
                    }
                ],
            },
        }
    )

    assert diagnostics.ok


@pytest.mark.parametrize("type_ref", ["List<Any", "Tuple<Any>", "Map<String>", 42])
def test_plugin_manifest_validation_rejects_malformed_type_expressions(type_ref: object) -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_type_expression"},
            "spec": {
                "pluginId": "com.example.bad_type_expression",
                "blocks": [
                    {
                        "typeId": "bad.block",
                        "version": 1,
                        "outputs": [{"name": "value", "type": type_ref}],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB0015"]


def test_plugin_manifest_validation_rejects_invalid_dict_resource_slot_schema_ids() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_resource_slot_schema_ref"},
            "spec": {
                "pluginId": "com.example.bad_resource_slot_schema_ref",
                "blocks": [
                    {
                        "typeId": "bad.block",
                        "version": 1,
                        "resourceSlots": {"store": {"type": "resources/VectorStore"}},
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB0015"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].resourceSlots.store.type",
    ]


@pytest.mark.parametrize("type_ref", ["???", "<>", ",", "."])
def test_plugin_manifest_validation_rejects_malformed_opaque_resource_types(
    type_ref: str,
) -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_opaque_resource"},
            "spec": {
                "pluginId": "com.example.bad_opaque_resource",
                "blocks": [
                    {
                        "typeId": "bad.resource",
                        "version": 1,
                        "resourceSlots": [{"name": "resource", "type": type_ref}],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB0015"]


def test_plugin_manifest_validation_rejects_non_boolean_contract_flags() -> None:
    diagnostics = validate_plugin_manifest(
        {
            "apiVersion": "graphblocks.ai/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"name": "com.example.bad_flags"},
            "spec": {
                "pluginId": "com.example.bad_flags",
                "blocks": [
                    {
                        "typeId": "bad.flags",
                        "version": 1,
                        "inputs": [{"name": "value", "type": "Any", "required": "false"}],
                        "resourceSlots": [
                            {
                                "name": "resource",
                                "type": "resources/Model@1",
                                "optional": "false",
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert not diagnostics.ok
    assert [item.code for item in diagnostics.diagnostics] == ["GB2015", "GB2015"]
