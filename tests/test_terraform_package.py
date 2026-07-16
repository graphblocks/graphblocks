from __future__ import annotations

import importlib
import json

import pytest


def _import_terraform(monkeypatch):
    return importlib.import_module("graphblocks.integrations.terraform")


def test_terraform_bridge_renders_tfvars_and_output_bindings(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = (
        graphblocks_deployment.ExecutionTarget("agent-workers", "worker_pool", "rust")
        .with_capabilities(["graphblocks.runtime"])
        .with_effects(["network"])
        .with_image("ghcr.io/acme/support-agent@sha256:runtime")
    )
    requirement = graphblocks_terraform.TerraformInfrastructureRequirement.for_execution_target(
        target,
        resource_type="kubernetes_deployment",
        resource_name="support_agent_workers",
        attributes={"namespace": "support"},
    )
    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        variables=(
            graphblocks_terraform.TerraformVariable("release_digest", "sha256:release"),
            graphblocks_terraform.TerraformVariable("worker_replicas", 3),
        ),
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("worker_url", "targets.agent_workers.url"),
        ),
        requirements=(requirement,),
    )

    assert json.loads(bridge.tfvars_json()) == {
        "release_digest": "sha256:release",
        "worker_replicas": 3,
    }
    assert bridge.requirement_contracts() == [
        {
            "target_id": "agent-workers",
            "target_kind": "worker_pool",
            "execution_host": "rust",
            "resource_type": "kubernetes_deployment",
            "resource_name": "support_agent_workers",
            "attributes": {"namespace": "support"},
            "capabilities": ["graphblocks.runtime"],
            "effects": ["network"],
            "image": "ghcr.io/acme/support-agent@sha256:runtime",
        }
    ]
    assert bridge.materialize_outputs({"worker_url": {"value": "https://workers.internal", "sensitive": False}}) == {
        "targets.agent_workers.url": "https://workers.internal"
    }


def test_terraform_bridge_digest_is_stable_across_input_order(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    left = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        variables=(
            graphblocks_terraform.TerraformVariable("b", 2),
            graphblocks_terraform.TerraformVariable("a", 1),
        ),
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("url", "service.url"),
            graphblocks_terraform.TerraformOutputBinding("id", "service.id"),
        ),
    )
    right = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        variables=(
            graphblocks_terraform.TerraformVariable("a", 1),
            graphblocks_terraform.TerraformVariable("b", 2),
        ),
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("id", "service.id"),
            graphblocks_terraform.TerraformOutputBinding("url", "service.url"),
        ),
    )

    assert left.tfvars_json() == '{"a":1,"b":2}'
    assert left.content_digest() == right.content_digest()


def test_terraform_bridge_rejects_duplicate_graphblocks_output_keys(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)

    with pytest.raises(
        graphblocks_terraform.TerraformBridgeError,
        match="graphblocks_key values must be unique",
    ):
        graphblocks_terraform.TerraformBridgeSpec(
            workspace="support-prod",
            output_bindings=(
                graphblocks_terraform.TerraformOutputBinding("primary_url", "service.url"),
                graphblocks_terraform.TerraformOutputBinding("fallback_url", "service.url"),
            ),
        )


def test_terraform_bridge_rejects_missing_required_output(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("worker_url", "targets.agent_workers.url"),
        ),
    )

    with pytest.raises(graphblocks_terraform.TerraformOutputMissingError, match="worker_url"):
        bridge.materialize_outputs({})


def test_terraform_bridge_materializes_sensitive_outputs_as_secret_refs(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding(
                "openai_api_key",
                "bindings.model.api_key",
                secret_ref="secret://support-prod/openai-api-key",
            ),
        ),
    )

    materialized = bridge.materialize_outputs(
        {"openai_api_key": {"value": "sk-should-not-leak", "sensitive": True}}
    )

    assert materialized == {
        "bindings.model.api_key": {
            "secretRef": "secret://support-prod/openai-api-key",
        }
    }
    assert "sk-should-not-leak" not in repr(materialized)


def test_terraform_bridge_imports_outputs_as_nested_binding_document(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("worker_url", "services.worker.url"),
            graphblocks_terraform.TerraformOutputBinding(
                "openai_api_key",
                "models.support.apiKey",
                secret_ref="secret://support-prod/openai-api-key",
            ),
        ),
    )

    document = bridge.materialize_binding_document(
        "support-production",
        {
            "worker_url": {"value": "https://workers.internal", "sensitive": False},
            "openai_api_key": {"value": "sk-should-not-leak", "sensitive": True},
        },
    )

    assert document == {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Binding",
        "metadata": {
            "name": "support-production",
            "annotations": {
                "graphblocks.ai/terraform-bridge-digest": bridge.content_digest(),
                "graphblocks.ai/terraform-workspace": "support-prod",
            },
        },
        "spec": {
            "models": {
                "support": {
                    "apiKey": {
                        "secretRef": "secret://support-prod/openai-api-key",
                    }
                }
            },
            "services": {
                "worker": {
                    "url": "https://workers.internal",
                }
            },
        },
    }
    assert "sk-should-not-leak" not in repr(document)
