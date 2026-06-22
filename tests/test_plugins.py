from __future__ import annotations

import yaml

from graphblocks import discover_plugins, validate_plugin_manifest


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

