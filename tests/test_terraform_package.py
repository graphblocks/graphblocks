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


def test_terraform_bridge_detaches_variables_and_fails_closed_on_sensitive_outputs(
    monkeypatch,
) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)
    value = {"regions": ["us-east-1"]}
    variable = graphblocks_terraform.TerraformVariable("deployment", value)
    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        variables=(variable,),
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding(
                "api_key",
                "models.support.apiKey",
            ),
        ),
    )
    value["regions"].append("caller-mutated")

    assert json.loads(bridge.tfvars_json()) == {
        "deployment": {"regions": ["us-east-1"]}
    }
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="requires secret_ref"):
        bridge.materialize_outputs(
            {"api_key": {"value": "secret", "sensitive": True}}
        )


def test_terraform_bridge_rejects_nonfinite_values_and_coerced_flags(
    monkeypatch,
) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)

    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="JSON-serializable"):
        graphblocks_terraform.TerraformVariable("threshold", float("nan"))
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="sensitive"):
        graphblocks_terraform.TerraformVariable(
            "token",
            "secret",
            sensitive="false",  # type: ignore[arg-type]
        )
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="required"):
        graphblocks_terraform.TerraformOutputBinding(
            "url",
            "service.url",
            required="true",  # type: ignore[arg-type]
        )


def test_terraform_bridge_rejects_ambiguous_paths_and_malformed_json_boundaries(
    monkeypatch,
) -> None:
    graphblocks_terraform = _import_terraform(monkeypatch)

    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="conflicting"):
        graphblocks_terraform.TerraformBridgeSpec(
            workspace="support-prod",
            output_bindings=(
                graphblocks_terraform.TerraformOutputBinding("service", "service"),
                graphblocks_terraform.TerraformOutputBinding("service_url", "service.url"),
            ),
        )
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="attribute keys"):
        graphblocks_terraform.TerraformInfrastructureRequirement(
            target_id="control",
            target_kind="service",
            execution_host="rust",
            resource_type="deployment",
            resource_name="control",
            attributes={1: "invalid"},  # type: ignore[dict-item]
        )
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="capabilities"):
        graphblocks_terraform.TerraformInfrastructureRequirement(
            target_id="control",
            target_kind="service",
            execution_host="rust",
            resource_type="deployment",
            resource_name="control",
            capabilities=(object(),),  # type: ignore[arg-type]
        )

    bridge = graphblocks_terraform.TerraformBridgeSpec(
        workspace="support-prod",
        output_bindings=(
            graphblocks_terraform.TerraformOutputBinding("worker_url", "service.url"),
        ),
    )
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="value"):
        bridge.materialize_outputs({"worker_url": {"sensitive": False}})
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="JSON"):
        bridge.materialize_outputs({"worker_url": object()})
    with pytest.raises(graphblocks_terraform.TerraformBridgeError, match="name"):
        bridge.materialize_binding_document(1, {"worker_url": "ok"})  # type: ignore[arg-type]
