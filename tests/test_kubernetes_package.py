from __future__ import annotations

import importlib
import json

import pytest
import yaml


def _import_kubernetes(monkeypatch):
    return importlib.import_module("graphblocks.integrations.kubernetes")


def test_kubernetes_adapter_renders_worker_deployment_and_service(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = (
        graphblocks_deployment.ExecutionTarget("agent-workers", "worker_pool", "rust")
        .with_capabilities(["graphblocks.runtime"])
        .with_effects(["network"])
        .with_image("ghcr.io/acme/support-agent@sha256:runtime")
    )
    options = graphblocks_kubernetes.KubernetesRenderOptions(
        namespace="support",
        labels={"app.kubernetes.io/part-of": "graphblocks"},
        annotations={"graphblocks.ai/release-id": "release-2026-06-23"},
        service_account_name="graphblocks-runtime",
    )
    ports = (graphblocks_kubernetes.KubernetesPort("http", 8080, service_port=80),)

    deployment = graphblocks_kubernetes.render_target_deployment(
        "support-agent",
        target,
        options=options,
        replicas=3,
        ports=ports,
        env={"GRAPHBLOCKS_RELEASE": "release-2026-06-23"},
    )
    service = graphblocks_kubernetes.render_target_service("support-agent", target, ports, options=options)

    assert deployment["apiVersion"] == "apps/v1"
    assert deployment["kind"] == "Deployment"
    assert deployment["metadata"]["namespace"] == "support"
    assert deployment["metadata"]["labels"]["app.kubernetes.io/part-of"] == "graphblocks"
    assert deployment["metadata"]["annotations"]["graphblocks.ai/target-id"] == "agent-workers"
    assert deployment["spec"]["replicas"] == 3
    assert deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/target-id": "agent-workers",
    }

    template_spec = deployment["spec"]["template"]["spec"]
    assert template_spec["serviceAccountName"] == "graphblocks-runtime"
    assert template_spec["containers"] == [
        {
            "name": "support-agent",
            "image": "ghcr.io/acme/support-agent@sha256:runtime",
            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
            "env": [{"name": "GRAPHBLOCKS_RELEASE", "value": "release-2026-06-23"}],
        }
    ]
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert service["spec"]["ports"] == [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}]


def test_kubernetes_adapter_renders_secret_env_references(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )

    deployment = graphblocks_kubernetes.render_target_deployment(
        "support-agent",
        target,
        env=(
            graphblocks_kubernetes.KubernetesEnv("GRAPHBLOCKS_RELEASE", "release-1"),
            graphblocks_kubernetes.KubernetesSecretEnv(
                "OPENAI_API_KEY",
                secret_name="model-provider-secrets",
                secret_key="openai-api-key",
            ),
        ),
    )

    assert deployment["spec"]["template"]["spec"]["containers"][0]["env"] == [
        {"name": "GRAPHBLOCKS_RELEASE", "value": "release-1"},
        {
            "name": "OPENAI_API_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "model-provider-secrets",
                    "key": "openai-api-key",
                }
            },
        },
    ]


def test_kubernetes_adapter_renders_canary_rollout_manifests(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    stable = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:stable",
    )
    candidate = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:candidate",
    )
    plan = graphblocks_deployment.RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(graphblocks_deployment.RolloutStep.canary("canary-10", traffic_percent=10),),
    )
    ports = (graphblocks_kubernetes.KubernetesPort("http", 8080),)

    manifest_set = graphblocks_kubernetes.render_rollout_manifests(
        "support-agent",
        stable,
        candidate,
        plan,
        active_step_index=2,
        options=graphblocks_kubernetes.KubernetesRenderOptions(namespace="support"),
        ports=ports,
        stable_replicas=9,
        candidate_replicas=1,
        env={"GRAPHBLOCKS_RELEASE": "release-1"},
    )
    stable_deployment, candidate_deployment = manifest_set.by_kind("Deployment")
    service = manifest_set.by_kind("Service")[0]

    assert stable_deployment["metadata"]["name"] == "support-agent-stable-v2"
    assert stable_deployment["spec"]["replicas"] == 9
    assert stable_deployment["metadata"]["annotations"]["graphblocks.ai/rollout-role"] == "stable"
    assert stable_deployment["metadata"]["annotations"]["graphblocks.ai/deployment-revision"] == "rev-stable"
    assert stable_deployment["metadata"]["annotations"]["graphblocks.ai/selector-version"] == "v2"
    assert stable_deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/rollout-role": "stable",
        "graphblocks.ai/selector-version": "v2",
    }
    assert "graphblocks.ai/rollout-id" not in stable_deployment["spec"]["selector"]["matchLabels"]
    assert stable_deployment["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/acme/support-agent@sha256:stable"
    )
    assert candidate_deployment["metadata"]["name"] == "support-agent-candidate-v2"
    assert candidate_deployment["spec"]["replicas"] == 1
    assert candidate_deployment["metadata"]["annotations"]["graphblocks.ai/rollout-step"] == "canary-10"
    assert candidate_deployment["metadata"]["annotations"]["graphblocks.ai/rollout-traffic-percent"] == "10"
    assert candidate_deployment["metadata"]["annotations"]["graphblocks.ai/deployment-revision"] == "rev-canary"
    assert candidate_deployment["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/rollout-role": "candidate",
        "graphblocks.ai/selector-version": "v2",
    }
    assert candidate_deployment["spec"]["template"]["metadata"]["labels"]["graphblocks.ai/rollout-role"] == "candidate"
    assert candidate_deployment["spec"]["template"]["metadata"]["labels"]["graphblocks.ai/rollout-id"] == (
        "rollout-1"
    )
    assert service["metadata"]["name"] == "support-agent"
    assert service["spec"]["selector"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/selector-version": "v2",
    }
    assert service["metadata"]["annotations"]["graphblocks.ai/rollout-step-kind"] == "canary"
    prune_targets = [target.contract() for target in manifest_set.prune_targets]
    assert prune_targets == [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "support-agent-candidate",
            "namespace": "support",
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "support-agent-stable",
            "namespace": "support",
        },
    ]
    assert manifest_set.by_kind("Job") == ()

    chart = graphblocks_kubernetes.render_helm_chart("support-agent", manifest_set)
    assert "graphblocks-prune-targets.json" in chart.file_names()
    assert json.loads(chart.file("graphblocks-prune-targets.json")) == {
        "pruneTargets": prune_targets,
    }
    assert "templates/graphblocks-prune-targets.json" not in chart.file_names()


def test_kubernetes_rollout_deployment_selectors_are_stable_across_rollouts(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    stable = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:stable",
    )
    candidate = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:candidate",
    )

    def deployment_contracts(rollout_id: str):
        plan = graphblocks_deployment.RolloutPlan.canary(
            rollout_id,
            "rev-stable",
            "rev-canary",
            canary_steps=(graphblocks_deployment.RolloutStep.canary("canary-10", traffic_percent=10),),
        )
        return graphblocks_kubernetes.render_rollout_manifests(
            "support-agent",
            stable,
            candidate,
            plan,
            active_step_index=2,
        ).by_kind("Deployment")

    first = deployment_contracts("rollout-1")
    second = deployment_contracts("rollout-2")

    assert [deployment["metadata"]["name"] for deployment in first] == [
        deployment["metadata"]["name"] for deployment in second
    ]
    assert [deployment["metadata"]["name"] for deployment in first] == [
        "support-agent-stable-v2",
        "support-agent-candidate-v2",
    ]
    assert [deployment["spec"]["selector"] for deployment in first] == [
        deployment["spec"]["selector"] for deployment in second
    ]
    assert [
        deployment["spec"]["template"]["metadata"]["labels"]["graphblocks.ai/rollout-id"]
        for deployment in first
    ] == ["rollout-1", "rollout-1"]
    assert [
        deployment["spec"]["template"]["metadata"]["labels"]["graphblocks.ai/rollout-id"]
        for deployment in second
    ] == ["rollout-2", "rollout-2"]


def test_kubernetes_rollout_v2_service_excludes_legacy_selector_pods(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    stable = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:stable",
    )
    candidate = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:candidate",
    )

    def render(rollout_id: str):
        plan = graphblocks_deployment.RolloutPlan.canary(
            rollout_id,
            "rev-stable",
            "rev-canary",
            canary_steps=(graphblocks_deployment.RolloutStep.canary("canary-10", traffic_percent=10),),
        )
        return graphblocks_kubernetes.render_rollout_manifests(
            "support-agent",
            stable,
            candidate,
            plan,
            active_step_index=2,
            ports=(graphblocks_kubernetes.KubernetesPort("http", 8080),),
        )

    previous = render("rollout-1")
    current = render("rollout-2")
    previous_service = previous.by_kind("Service")[0]
    current_service = current.by_kind("Service")[0]
    service_selector = current_service["spec"]["selector"]
    legacy_pod_labels = {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/rollout-id": "rollout-1",
        "graphblocks.ai/rollout-role": "stable",
    }

    assert previous_service["spec"]["selector"] == current_service["spec"]["selector"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/selector-version": "v2",
    }
    assert previous_service["metadata"]["annotations"]["graphblocks.ai/rollout-id"] == "rollout-1"
    assert current_service["metadata"]["annotations"]["graphblocks.ai/rollout-id"] == "rollout-2"
    assert not all(legacy_pod_labels.get(key) == value for key, value in service_selector.items())
    for manifests in (previous, current):
        for deployment in manifests.by_kind("Deployment"):
            v2_pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
            assert all(v2_pod_labels.get(key) == value for key, value in service_selector.items())


def test_kubernetes_rollout_service_routes_only_candidate_after_promote(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    stable = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:stable",
    )
    candidate = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:candidate",
    )
    plan = graphblocks_deployment.RolloutPlan.canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        canary_steps=(graphblocks_deployment.RolloutStep.canary("canary-25", traffic_percent=25),),
    )

    manifest_set = graphblocks_kubernetes.render_rollout_manifests(
        "support-agent",
        stable,
        candidate,
        plan,
        active_step_index=3,
        options=graphblocks_kubernetes.KubernetesRenderOptions(namespace="support"),
        ports=(graphblocks_kubernetes.KubernetesPort("http", 8080),),
    )
    stable_deployment, candidate_deployment = manifest_set.by_kind("Deployment")
    service = manifest_set.by_kind("Service")[0]

    assert stable_deployment["spec"]["replicas"] == 0
    assert candidate_deployment["spec"]["replicas"] == 1
    assert service["spec"]["selector"] == {
        "app.kubernetes.io/name": "support-agent",
        "graphblocks.ai/rollout-role": "candidate",
        "graphblocks.ai/selector-version": "v2",
    }


def test_kubernetes_manifest_set_digest_is_independent_of_document_order(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    options = graphblocks_kubernetes.KubernetesRenderOptions(namespace="support")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )
    ports = (graphblocks_kubernetes.KubernetesPort("http", 8080),)
    deployment = graphblocks_kubernetes.render_target_deployment(
        "support-agent",
        target,
        options=options,
        ports=ports,
    )
    service = graphblocks_kubernetes.render_target_service("support-agent", target, ports, options=options)

    left = graphblocks_kubernetes.KubernetesManifestSet((deployment, service))
    right = graphblocks_kubernetes.KubernetesManifestSet((service, deployment))

    assert left.content_digest() == right.content_digest()
    assert left.prune_targets == ()
    assert [manifest["kind"] for manifest in left.by_kind("Deployment")] == ["Deployment"]
    assert [manifest["kind"] for manifest in left.by_kind("Service")] == ["Service"]
    digest_before_public_mutation = left.content_digest()
    left.documents[0]["metadata"]["name"] = "mutated"  # type: ignore[index]
    assert left.content_digest() == digest_before_public_mutation
    assert left.documents[0]["metadata"]["name"] != "mutated"  # type: ignore[index]

    obsolete = graphblocks_kubernetes.KubernetesPruneTarget(
        "apps/v1",
        "Deployment",
        "support-agent-stable",
        "support",
    )
    pruned_left = graphblocks_kubernetes.KubernetesManifestSet(
        (deployment, service),
        prune_targets=(obsolete,),
    )
    pruned_right = graphblocks_kubernetes.KubernetesManifestSet(
        (service, deployment),
        prune_targets=(obsolete,),
    )
    assert pruned_left.content_digest() == pruned_right.content_digest()
    assert pruned_left.content_digest() != left.content_digest()


def test_kubernetes_cluster_snapshot_tracks_capabilities_deterministically(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    deployment = graphblocks_kubernetes.KubernetesClusterCapability("apps/v1", "Deployment")
    service = graphblocks_kubernetes.KubernetesClusterCapability("v1", "Service")

    left = graphblocks_kubernetes.KubernetesClusterSnapshot(
        cluster_id="prod-us",
        server_version="1.30",
        capabilities=(deployment, service),
        namespaces=("support", "graphblocks-system"),
        runtime_classes=("gvisor", "runc"),
    )
    right = graphblocks_kubernetes.KubernetesClusterSnapshot(
        cluster_id="prod-us",
        server_version="1.30",
        capabilities=(service, deployment),
        namespaces=("graphblocks-system", "support"),
        runtime_classes=("runc", "gvisor"),
    )

    assert left.supports("apps/v1", "Deployment")
    assert not left.supports("batch/v1", "CronJob")
    assert left.content_digest() == right.content_digest()


def test_kubernetes_adapter_renders_helm_chart_package(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )
    manifest_set = graphblocks_kubernetes.render_target_manifests(
        "support-agent",
        target,
        options=graphblocks_kubernetes.KubernetesRenderOptions(namespace="support"),
        ports=(graphblocks_kubernetes.KubernetesPort("http", 8080, service_port=80),),
        env={"GRAPHBLOCKS_RELEASE": "release-2026-06-24"},
    )

    chart = graphblocks_kubernetes.render_helm_chart(
        "support-agent",
        manifest_set,
        chart_version="1.2.3",
        app_version="2026.06.24.1",
        values={
            "deploymentRevisionId": "rev-1",
            "releaseDigest": "sha256:release",
        },
    )

    assert chart.file_names() == (
        "Chart.yaml",
        "templates/support-agent-deployment.yaml",
        "templates/support-agent-service.yaml",
        "values.yaml",
    )
    assert yaml.safe_load(chart.file("Chart.yaml")) == {
        "apiVersion": "v2",
        "name": "support-agent",
        "description": "GraphBlocks deployment chart for support-agent",
        "type": "application",
        "version": "1.2.3",
        "appVersion": "2026.06.24.1",
    }
    assert yaml.safe_load(chart.file("values.yaml")) == {
        "deploymentRevisionId": "rev-1",
        "releaseDigest": "sha256:release",
    }
    deployment = yaml.safe_load(chart.file("templates/support-agent-deployment.yaml"))
    assert deployment["kind"] == "Deployment"
    assert deployment["metadata"]["namespace"] == "support"
    assert deployment["metadata"]["annotations"]["graphblocks.ai/target-id"] == "agent-workers"
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/acme/support-agent@sha256:runtime"
    )
    assert chart.content_digest().startswith("sha256:")


def test_kubernetes_adapter_renders_callback_ingress_gateway_manifests(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    options = graphblocks_kubernetes.KubernetesRenderOptions(
        namespace="support",
        labels={"app.kubernetes.io/part-of": "graphblocks"},
    )

    manifest_set = graphblocks_kubernetes.render_callback_ingress_manifests(
        "callback-gateway",
        {
            "enabled": True,
            "routes": [
                {
                    "path": "/v1/callbacks/{operation_id}",
                    "command": "SubmitAsyncCallback",
                }
            ],
            "security": {
                "requireSignature": True,
                "antiEnumeration": True,
            },
            "limits": {
                "maxPayloadBytes": 262144,
                "maxRequestsPerSecond": 100,
            },
        },
        service_name="graphblocks-server",
        service_port=8080,
        parent_refs=[{"name": "graphblocks-public", "namespace": "gateways"}],
        options=options,
    )

    service = manifest_set.by_kind("Service")[0]
    route = manifest_set.by_kind("HTTPRoute")[0]
    network_policy = manifest_set.by_kind("NetworkPolicy")[0]

    assert service == {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "callback-gateway",
            "namespace": "support",
            "labels": {
                "app.kubernetes.io/component": "callback-gateway",
                "app.kubernetes.io/managed-by": "graphblocks",
                "app.kubernetes.io/name": "callback-gateway",
                "app.kubernetes.io/part-of": "graphblocks",
            },
            "annotations": {
                "graphblocks.ai/callback-ingress": "true",
                "graphblocks.ai/max-payload-bytes": "262144",
                "graphblocks.ai/max-requests-per-second": "100",
                "graphblocks.ai/require-signature": "true",
                "graphblocks.ai/anti-enumeration": "true",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": "graphblocks-server",
            },
            "ports": [
                {
                    "name": "http",
                    "port": 8080,
                    "targetPort": 8080,
                    "protocol": "TCP",
                }
            ],
        },
    }
    assert route["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert route["metadata"]["name"] == "callback-gateway"
    assert route["spec"]["parentRefs"] == [{"name": "graphblocks-public", "namespace": "gateways"}]
    assert route["spec"]["rules"] == [
        {
            "matches": [
                {
                    "path": {
                        "type": "PathPrefix",
                        "value": "/v1/callbacks/",
                    },
                    "method": "POST",
                }
            ],
            "backendRefs": [
                {
                    "name": "callback-gateway",
                    "port": 8080,
                }
            ],
            "filters": [
                {
                    "type": "RequestHeaderModifier",
                    "requestHeaderModifier": {
                        "set": [
                            {
                                "name": "GraphBlocks-Callback-Command",
                                "value": "SubmitAsyncCallback",
                            }
                        ]
                    },
                }
            ],
        }
    ]
    assert network_policy["spec"]["policyTypes"] == ["Ingress"]
    assert network_policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert manifest_set.content_digest().startswith("sha256:")


def test_kubernetes_callback_ingress_renderer_rejects_unsigned_enabled_ingress(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)

    try:
        graphblocks_kubernetes.render_callback_ingress_manifests(
            "callback-gateway",
            {
                "enabled": True,
                "routes": [{"path": "/v1/callbacks/{operation_id}", "command": "SubmitAsyncCallback"}],
                "security": {"requireSignature": False},
            },
            service_name="graphblocks-server",
        )
    except graphblocks_kubernetes.KubernetesAdapterError as error:
        assert "GB6002" in str(error)
    else:
        raise AssertionError("unsigned enabled callback ingress must be rejected")


def test_kubernetes_callback_routes_use_exact_or_route_specific_dynamic_matches(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)

    manifest_set = graphblocks_kubernetes.render_callback_ingress_manifests(
        "callback-gateway",
        {
            "enabled": True,
            "routes": [
                {
                    "method": "POST",
                    "path": "/v1/callbacks/{operation_id}",
                    "command": "SubmitAsyncCallback",
                },
                {
                    "method": "POST",
                    "path": "/v1/callbacks/register",
                    "command": "RegisterCallback",
                },
                {
                    "method": "POST",
                    "path": "/v1/callbacks/deliveries/{delivery_id}/redrive",
                    "command": "RedriveCallbackDelivery",
                },
                {
                    "method": "POST",
                    "path": "/v1/callbacks/deliveries/{delivery_id}/dead-letter",
                    "command": "DeadLetterCallbackDelivery",
                },
            ],
        },
        service_name="graphblocks-server",
        parent_refs=({"name": "graphblocks-public"},),
        options=graphblocks_kubernetes.KubernetesRenderOptions(
            gateway_regex_path_matches=True,
        ),
    )

    matches = [rule["matches"][0] for rule in manifest_set.by_kind("HTTPRoute")[0]["spec"]["rules"]]
    assert matches == [
        {
            "path": {
                "type": "PathPrefix",
                "value": "/v1/callbacks/",
            },
            "method": "POST",
        },
        {
            "path": {"type": "Exact", "value": "/v1/callbacks/register"},
            "method": "POST",
        },
        {
            "path": {
                "type": "RegularExpression",
                "value": "^/v1/callbacks/deliveries/[^/]+/redrive$",
            },
            "method": "POST",
        },
        {
            "path": {
                "type": "RegularExpression",
                "value": "^/v1/callbacks/deliveries/[^/]+/dead\\-letter$",
            },
            "method": "POST",
        },
    ]
    commands = [
        rule["filters"][0]["requestHeaderModifier"]["set"][0]["value"]
        for rule in manifest_set.by_kind("HTTPRoute")[0]["spec"]["rules"]
    ]
    assert commands == [
        "SubmitAsyncCallback",
        "RegisterCallback",
        "RedriveCallbackDelivery",
        "DeadLetterCallbackDelivery",
    ]

    with pytest.raises(
        graphblocks_kubernetes.KubernetesAdapterError,
        match="gateway_regex_path_matches",
    ):
        graphblocks_kubernetes.render_callback_ingress_manifests(
            "callback-gateway",
            {
                "enabled": True,
                "routes": [
                    {
                        "method": "POST",
                        "path": "/v1/callbacks/{operation_id}",
                        "command": "SubmitAsyncCallback",
                    },
                    {
                        "method": "POST",
                        "path": "/v1/callbacks/deliveries/{delivery_id}/redrive",
                        "command": "RedriveCallbackDelivery",
                    },
                ],
            },
            service_name="graphblocks-server",
            parent_refs=({"name": "graphblocks-public"},),
        )


def test_kubernetes_full_canary_routes_to_safely_sized_candidate(monkeypatch) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    stable = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:stable",
    )
    candidate = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:candidate",
    )
    plan = graphblocks_deployment.RolloutPlan.canary(
        "rollout-100",
        "rev-stable",
        "rev-canary",
        canary_steps=(
            graphblocks_deployment.RolloutStep.canary("canary-100", traffic_percent=100),
        ),
    )

    manifest_set = graphblocks_kubernetes.render_rollout_manifests(
        "support-agent",
        stable,
        candidate,
        plan,
        active_step_index=2,
        stable_replicas=10,
        ports=(graphblocks_kubernetes.KubernetesPort("http", 8080),),
    )
    stable_deployment, candidate_deployment = manifest_set.by_kind("Deployment")
    service = manifest_set.by_kind("Service")[0]

    assert stable_deployment["spec"]["replicas"] == 10
    assert candidate_deployment["spec"]["replicas"] == 10
    assert service["spec"]["selector"]["graphblocks.ai/rollout-role"] == "candidate"


def test_kubernetes_adapter_rejects_coerced_numeric_and_boolean_fields(
    monkeypatch,
) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )

    for port in (True, 8080.5):
        with pytest.raises(
            graphblocks_kubernetes.KubernetesAdapterError,
            match="integer between",
        ):
            graphblocks_kubernetes.KubernetesPort(
                "http",
                port,  # type: ignore[arg-type]
            )
    for replicas in (True, 1.5):
        with pytest.raises(
            graphblocks_kubernetes.KubernetesAdapterError,
            match="non-negative integer",
        ):
            graphblocks_kubernetes.render_target_deployment(
                "support-agent",
                target,
                replicas=replicas,  # type: ignore[arg-type]
            )
    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="optional"):
        graphblocks_kubernetes.KubernetesSecretEnv(
            "TOKEN",
            "runtime-secrets",
            "token",
            optional="false",  # type: ignore[arg-type]
        )
    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="namespaced"):
        graphblocks_kubernetes.KubernetesClusterCapability(
            "apps/v1",
            "Deployment",
            namespaced="true",  # type: ignore[arg-type]
        )
    with pytest.raises(
        graphblocks_kubernetes.KubernetesAdapterError,
        match="include_network_policy",
    ):
        graphblocks_kubernetes.render_callback_ingress_manifests(
            "callbacks",
            {"enabled": False},
            service_name="callback-worker",
            include_network_policy="false",  # type: ignore[arg-type]
        )


def test_kubernetes_manifest_set_rejects_duplicate_or_pruned_active_identity(
    monkeypatch,
) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    document = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "worker", "namespace": "support"},
    }

    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="duplicate"):
        graphblocks_kubernetes.KubernetesManifestSet((document, document))
    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="active"):
        graphblocks_kubernetes.KubernetesManifestSet(
            (document,),
            prune_targets=(
                graphblocks_kubernetes.KubernetesPruneTarget(
                    "apps/v1",
                    "Deployment",
                    "worker",
                    "support",
                ),
            ),
        )


def test_kubernetes_renderer_rejects_duplicate_container_contracts(
    monkeypatch,
) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)
    graphblocks_deployment = importlib.import_module("graphblocks.deployment")
    target = graphblocks_deployment.ExecutionTarget(
        "agent-workers",
        "worker_pool",
        "rust",
        image="ghcr.io/acme/support-agent@sha256:runtime",
    )

    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="port names"):
        graphblocks_kubernetes.render_target_deployment(
            "support-agent",
            target,
            ports=(
                graphblocks_kubernetes.KubernetesPort("http", 8080),
                graphblocks_kubernetes.KubernetesPort("http", 8081),
            ),
        )
    with pytest.raises(
        graphblocks_kubernetes.KubernetesAdapterError,
        match="environment variable names",
    ):
        graphblocks_kubernetes.render_target_deployment(
            "support-agent",
            target,
            env=(
                graphblocks_kubernetes.KubernetesEnv("TOKEN", "one"),
                graphblocks_kubernetes.KubernetesEnv("TOKEN", "two"),
            ),
        )


def test_kubernetes_callback_and_helm_paths_reject_ambiguous_inputs(
    monkeypatch,
) -> None:
    graphblocks_kubernetes = _import_kubernetes(monkeypatch)

    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="conflicting"):
        graphblocks_kubernetes.render_callback_ingress_manifests(
            "callbacks",
            {
                "enabled": True,
                "routes": [
                    {
                        "path": "/callbacks/{callback_id}",
                        "command": "SubmitAsyncCallback",
                    }
                ],
                "security": {
                    "requireSignature": True,
                    "require_signature": False,
                },
            },
            service_name="callback-worker",
            parent_refs=({"name": "gateway"},),
        )
    with pytest.raises(graphblocks_kubernetes.KubernetesAdapterError, match="path"):
        graphblocks_kubernetes.HelmChartFile(r"..\Chart.yaml", "unsafe")
