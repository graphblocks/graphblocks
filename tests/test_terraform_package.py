from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _import_terraform(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-deployment" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-terraform" / "src"))
    return importlib.import_module("graphblocks_terraform")


def test_terraform_bridge_renders_tfvars_and_output_bindings(monkeypatch) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks_deployment")
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
