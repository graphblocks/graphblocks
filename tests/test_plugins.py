from __future__ import annotations

import yaml

from graphblocks import BlockCatalog, discover_plugins, validate_plugin_manifest


def test_builtin_plugin_discovery_does_not_need_installed_scan() -> None:
    registry = discover_plugins(include_installed=False)

    assert registry.ok
    assert [manifest.plugin_id for manifest in registry.manifests] == ["io.graphblocks.stdlib"]


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
    control_map = catalog.get("control.map@2")

    assert prompt is not None
    assert [port.name for port in prompt.inputs] == ["message"]
    assert [port.name for port in prompt.outputs] == ["prompt"]
    assert model is not None
    assert [port.name for port in model.inputs] == ["prompt"]
    assert [port.name for port in model.outputs] == ["response"]
    assert control_map is not None
    assert [port.name for port in control_map.inputs] == ["items"]
    assert [port.name for port in control_map.outputs] == ["values", "outcomes"]


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
    assert [item.code for item in diagnostics.diagnostics] == ["InvalidSchemaId", "InvalidSchemaId"]
    assert [item.path for item in diagnostics.diagnostics] == [
        "$.spec.blocks[0].inputs[0].type",
        "$.spec.blocks[0].resourceSlots[0].type",
    ]
