from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Literal

from graphblocks.deployment import ExecutionTarget, RolloutPlan


KubernetesProtocol = Literal["TCP", "UDP", "SCTP"]
KubernetesManifest = dict[str, object]


class KubernetesAdapterError(ValueError):
    """Raised when a Kubernetes manifest contract cannot be rendered."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


def _sorted_string_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values}))


def _selector_labels(name: str, target: ExecutionTarget) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": name,
        "graphblocks.ai/target-id": target.target_id,
    }


def _workload_labels(
    name: str,
    target: ExecutionTarget,
    options: KubernetesRenderOptions,
) -> dict[str, str]:
    labels = {
        **options.labels,
        "app.kubernetes.io/component": target.kind.replace("_", "-"),
        "app.kubernetes.io/managed-by": "graphblocks",
        **_selector_labels(name, target),
    }
    return {key: labels[key] for key in sorted(labels)}


def _target_annotations(target: ExecutionTarget) -> dict[str, str]:
    annotations = {
        "graphblocks.ai/execution-host": target.execution_host,
        "graphblocks.ai/target-id": target.target_id,
        "graphblocks.ai/target-kind": target.kind,
    }
    if target.image is not None:
        annotations["graphblocks.ai/image"] = target.image
    if target.package_lock is not None:
        annotations["graphblocks.ai/package-lock"] = target.package_lock
    return {key: annotations[key] for key in sorted(annotations)}


def _object_metadata(
    name: str,
    options: KubernetesRenderOptions,
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
) -> dict[str, object]:
    merged_annotations = {**options.annotations, **annotations}
    return {
        "name": name,
        "namespace": options.namespace,
        "labels": {key: labels[key] for key in sorted(labels)},
        "annotations": {key: merged_annotations[key] for key in sorted(merged_annotations)},
    }


def _env_contracts(env: Mapping[str, str] | Iterable[KubernetesEnv | KubernetesSecretEnv]) -> list[dict[str, object]]:
    if isinstance(env, Mapping):
        return [
            KubernetesEnv(str(key), str(value)).env_contract()
            for key, value in sorted(env.items())
        ]
    return [item.env_contract() for item in sorted(tuple(env), key=lambda env_item: env_item.name)]


@dataclass(frozen=True, slots=True)
class KubernetesRenderOptions:
    namespace: str = "default"
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)
    service_account_name: str | None = None
    image_pull_secrets: tuple[str, ...] = field(default_factory=tuple)
    runtime_class_name: str | None = None
    gateway_regex_path_matches: bool = False

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise KubernetesAdapterError("namespace must not be empty")
        if not isinstance(self.gateway_regex_path_matches, bool):
            raise KubernetesAdapterError("gateway_regex_path_matches must be a boolean")
        object.__setattr__(
            self,
            "labels",
            {str(key): str(value) for key, value in sorted(dict(self.labels).items())},
        )
        object.__setattr__(
            self,
            "annotations",
            {str(key): str(value) for key, value in sorted(dict(self.annotations).items())},
        )
        object.__setattr__(self, "image_pull_secrets", _sorted_string_tuple(self.image_pull_secrets))


@dataclass(frozen=True, slots=True)
class KubernetesPort:
    name: str
    container_port: int
    service_port: int | None = None
    protocol: KubernetesProtocol = "TCP"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise KubernetesAdapterError("port name must not be empty")
        if self.protocol not in {"TCP", "UDP", "SCTP"}:
            raise KubernetesAdapterError(f"unsupported Kubernetes protocol: {self.protocol!r}")
        for field_name, port in (("container_port", self.container_port), ("service_port", self.service_port)):
            if port is not None and not 1 <= port <= 65535:
                raise KubernetesAdapterError(f"{field_name} must be between 1 and 65535")

    def container_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "containerPort": self.container_port,
            "protocol": self.protocol,
        }

    def service_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "port": self.service_port or self.container_port,
            "targetPort": self.name,
            "protocol": self.protocol,
        }


@dataclass(frozen=True, slots=True)
class KubernetesEnv:
    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise KubernetesAdapterError("environment variable name must not be empty")

    def env_contract(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True, slots=True)
class KubernetesSecretEnv:
    name: str
    secret_name: str
    secret_key: str
    optional: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("name", self.name),
            ("secret_name", self.secret_name),
            ("secret_key", self.secret_key),
        ):
            if not value.strip():
                raise KubernetesAdapterError(f"{field_name} must not be empty")

    def env_contract(self) -> dict[str, object]:
        secret_key_ref: dict[str, object] = {
            "name": self.secret_name,
            "key": self.secret_key,
        }
        if self.optional:
            secret_key_ref["optional"] = True
        return {
            "name": self.name,
            "valueFrom": {"secretKeyRef": secret_key_ref},
        }


@dataclass(frozen=True, slots=True)
class KubernetesManifestSet:
    documents: tuple[KubernetesManifest, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "documents", tuple(deepcopy(document) for document in self.documents))

    def by_kind(self, kind: str) -> tuple[KubernetesManifest, ...]:
        return tuple(deepcopy(document) for document in self.documents if document.get("kind") == kind)

    def content_digest(self) -> str:
        documents = [deepcopy(document) for document in self.documents]
        documents.sort(key=_canonical_dumps)
        return _content_digest({"documents": documents})


@dataclass(frozen=True, slots=True)
class HelmChartFile:
    path: str
    content: str

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise KubernetesAdapterError("Helm chart file path must not be empty")
        path_parts = self.path.split("/")
        if self.path.startswith("/") or any(part in {"", ".", ".."} for part in path_parts):
            raise KubernetesAdapterError(f"invalid Helm chart file path: {self.path!r}")
        object.__setattr__(self, "content", str(self.content))


@dataclass(frozen=True, slots=True)
class HelmChartPackage:
    chart_name: str
    files: tuple[HelmChartFile, ...]

    def __post_init__(self) -> None:
        if not self.chart_name.strip():
            raise KubernetesAdapterError("Helm chart name must not be empty")
        files = tuple(sorted(self.files, key=lambda file: file.path))
        paths = [file.path for file in files]
        if len(paths) != len(set(paths)):
            raise KubernetesAdapterError("Helm chart file paths must be unique")
        required_files = {"Chart.yaml", "values.yaml"}
        missing_files = required_files.difference(paths)
        if missing_files:
            missing = ", ".join(sorted(missing_files))
            raise KubernetesAdapterError(f"Helm chart package is missing required file(s): {missing}")
        object.__setattr__(self, "files", files)

    def file_names(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)

    def file(self, path: str) -> str:
        for file in self.files:
            if file.path == path:
                return file.content
        raise KubernetesAdapterError(f"Helm chart file {path!r} not found")

    def file_map(self) -> dict[str, str]:
        return {file.path: file.content for file in self.files}

    def content_digest(self) -> str:
        return _content_digest(
            {
                "chart_name": self.chart_name,
                "files": [{"path": file.path, "content": file.content} for file in self.files],
            }
        )


@dataclass(frozen=True, slots=True)
class KubernetesClusterCapability:
    api_version: str
    kind: str
    namespaced: bool = True
    verbs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.api_version.strip():
            raise KubernetesAdapterError("api_version must not be empty")
        if not self.kind.strip():
            raise KubernetesAdapterError("kind must not be empty")
        object.__setattr__(self, "verbs", _sorted_string_tuple(self.verbs))

    def canonical_value(self) -> dict[str, object]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "namespaced": self.namespaced,
            "verbs": list(self.verbs),
        }


@dataclass(frozen=True, slots=True)
class KubernetesClusterSnapshot:
    cluster_id: str
    server_version: str | None = None
    capabilities: tuple[KubernetesClusterCapability, ...] = field(default_factory=tuple)
    namespaces: tuple[str, ...] = field(default_factory=tuple)
    runtime_classes: tuple[str, ...] = field(default_factory=tuple)
    storage_classes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.cluster_id.strip():
            raise KubernetesAdapterError("cluster_id must not be empty")
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(self.capabilities, key=lambda capability: _canonical_dumps(capability.canonical_value()))),
        )
        object.__setattr__(self, "namespaces", _sorted_string_tuple(self.namespaces))
        object.__setattr__(self, "runtime_classes", _sorted_string_tuple(self.runtime_classes))
        object.__setattr__(self, "storage_classes", _sorted_string_tuple(self.storage_classes))

    def supports(self, api_version: str, kind: str) -> bool:
        return any(
            capability.api_version == api_version and capability.kind == kind
            for capability in self.capabilities
        )

    def content_digest(self) -> str:
        return _content_digest(
            {
                "cluster_id": self.cluster_id,
                "server_version": self.server_version,
                "capabilities": [capability.canonical_value() for capability in self.capabilities],
                "namespaces": list(self.namespaces),
                "runtime_classes": list(self.runtime_classes),
                "storage_classes": list(self.storage_classes),
            }
        )


def render_target_deployment(
    name: str,
    target: ExecutionTarget,
    *,
    options: KubernetesRenderOptions | None = None,
    replicas: int = 1,
    image: str | None = None,
    ports: Iterable[KubernetesPort] = (),
    env: Mapping[str, str] | Iterable[KubernetesEnv | KubernetesSecretEnv] = (),
    command: Iterable[str] = (),
    args: Iterable[str] = (),
    resources: Mapping[str, object] | None = None,
) -> KubernetesManifest:
    if not name.strip():
        raise KubernetesAdapterError("deployment name must not be empty")
    if replicas < 0:
        raise KubernetesAdapterError("replicas must not be negative")
    options = options or KubernetesRenderOptions()
    deployment_image = image or target.image
    if deployment_image is None or not deployment_image.strip():
        raise KubernetesAdapterError("target image must be provided")

    port_contracts = [port.container_contract() for port in ports]
    env_contracts = _env_contracts(env)

    container: dict[str, object] = {
        "name": name,
        "image": deployment_image,
    }
    if port_contracts:
        container["ports"] = port_contracts
    if env_contracts:
        container["env"] = env_contracts
    command = tuple(command)
    args = tuple(args)
    if command:
        container["command"] = list(command)
    if args:
        container["args"] = list(args)
    if resources is not None:
        container["resources"] = deepcopy(dict(resources))

    pod_spec: dict[str, object] = {"containers": [container]}
    if options.service_account_name is not None:
        pod_spec["serviceAccountName"] = options.service_account_name
    if options.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [{"name": secret} for secret in options.image_pull_secrets]
    if options.runtime_class_name is not None:
        pod_spec["runtimeClassName"] = options.runtime_class_name

    selector = _selector_labels(name, target)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _object_metadata(
            name,
            options,
            _workload_labels(name, target, options),
            _target_annotations(target),
        ),
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": _workload_labels(name, target, options),
                    "annotations": _target_annotations(target),
                },
                "spec": pod_spec,
            },
        },
    }


def render_target_service(
    name: str,
    target: ExecutionTarget,
    ports: Iterable[KubernetesPort],
    *,
    options: KubernetesRenderOptions | None = None,
    service_type: str = "ClusterIP",
) -> KubernetesManifest:
    if not name.strip():
        raise KubernetesAdapterError("service name must not be empty")
    options = options or KubernetesRenderOptions()
    service_ports = [port.service_contract() for port in ports]
    if not service_ports:
        raise KubernetesAdapterError("service must expose at least one port")
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _object_metadata(
            name,
            options,
            _workload_labels(name, target, options),
            _target_annotations(target),
        ),
        "spec": {
            "type": service_type,
            "selector": _selector_labels(name, target),
            "ports": service_ports,
        },
    }


def render_rollout_manifests(
    name: str,
    stable_target: ExecutionTarget,
    candidate_target: ExecutionTarget,
    rollout_plan: RolloutPlan,
    *,
    active_step_index: int,
    options: KubernetesRenderOptions | None = None,
    ports: Iterable[KubernetesPort] = (),
    env: Mapping[str, str] | Iterable[KubernetesEnv | KubernetesSecretEnv] = (),
    stable_replicas: int = 1,
    candidate_replicas: int | None = None,
) -> KubernetesManifestSet:
    if not name.strip():
        raise KubernetesAdapterError("rollout name must not be empty")
    if stable_replicas < 0:
        raise KubernetesAdapterError("stable_replicas must not be negative")
    if candidate_replicas is not None and candidate_replicas < 0:
        raise KubernetesAdapterError("candidate_replicas must not be negative")
    options = options or KubernetesRenderOptions()
    ports = tuple(ports)
    env_contracts = _env_contracts(env)
    active_step = rollout_plan.current_step(active_step_index)

    stable_image = stable_target.image
    candidate_image = candidate_target.image
    if stable_image is None or not stable_image.strip():
        raise KubernetesAdapterError("stable target image must be provided")
    if candidate_image is None or not candidate_image.strip():
        raise KubernetesAdapterError("candidate target image must be provided")

    if active_step.kind == "promote":
        effective_stable_replicas = 0
        effective_candidate_replicas = stable_replicas if candidate_replicas is None else candidate_replicas
        service_role = "candidate"
    elif active_step.kind == "canary":
        effective_stable_replicas = stable_replicas
        if candidate_replicas is not None:
            effective_candidate_replicas = candidate_replicas
        elif active_step.traffic_percent == 100:
            effective_candidate_replicas = stable_replicas
        elif active_step.traffic_percent <= 0:
            effective_candidate_replicas = 0
        else:
            denominator = max(1, 100 - active_step.traffic_percent)
            effective_candidate_replicas = max(
                1,
                (stable_replicas * active_step.traffic_percent + denominator - 1) // denominator,
            )
        if active_step.traffic_percent == 100:
            service_role = "candidate"
        else:
            service_role = None if active_step.traffic_percent > 0 else "stable"
    elif active_step.kind == "shadow":
        effective_stable_replicas = stable_replicas
        effective_candidate_replicas = 1 if candidate_replicas is None else candidate_replicas
        service_role = "stable"
    else:
        effective_stable_replicas = stable_replicas
        effective_candidate_replicas = 0 if candidate_replicas is None else candidate_replicas
        service_role = "stable"

    documents: list[KubernetesManifest] = []
    for role, target, revision_id, replicas, image in (
        ("stable", stable_target, rollout_plan.stable_revision_id, effective_stable_replicas, stable_image),
        (
            "candidate",
            candidate_target,
            rollout_plan.candidate_revision_id,
            effective_candidate_replicas,
            candidate_image,
        ),
    ):
        selector = {
            "app.kubernetes.io/name": name,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
            "graphblocks.ai/rollout-role": role,
        }
        labels = {
            **options.labels,
            "app.kubernetes.io/component": target.kind.replace("_", "-"),
            "app.kubernetes.io/managed-by": "graphblocks",
            "app.kubernetes.io/name": name,
            "graphblocks.ai/deployment-revision": revision_id,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
            "graphblocks.ai/rollout-role": role,
            "graphblocks.ai/target-id": target.target_id,
        }
        annotations = {
            **_target_annotations(target),
            "graphblocks.ai/deployment-revision": revision_id,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
            "graphblocks.ai/rollout-role": role,
            "graphblocks.ai/rollout-step": active_step.step_id,
            "graphblocks.ai/rollout-step-kind": active_step.kind,
            "graphblocks.ai/rollout-traffic-percent": str(active_step.traffic_percent),
        }
        container: dict[str, object] = {
            "name": name,
            "image": image,
        }
        port_contracts = [port.container_contract() for port in ports]
        if port_contracts:
            container["ports"] = port_contracts
        if env_contracts:
            container["env"] = env_contracts

        documents.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": _object_metadata(
                    f"{name}-{role}",
                    options,
                    labels,
                    annotations,
                ),
                "spec": {
                    "replicas": replicas,
                    "selector": {"matchLabels": selector},
                    "template": {
                        "metadata": {
                            "labels": {key: labels[key] for key in sorted(labels)},
                            "annotations": {key: annotations[key] for key in sorted(annotations)},
                        },
                        "spec": {"containers": [container]},
                    },
                },
            }
        )

    if ports:
        service_selector = {
            "app.kubernetes.io/name": name,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
        }
        if service_role is not None:
            service_selector["graphblocks.ai/rollout-role"] = service_role
        service_annotations = {
            "graphblocks.ai/candidate-revision": rollout_plan.candidate_revision_id,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
            "graphblocks.ai/rollout-step": active_step.step_id,
            "graphblocks.ai/rollout-step-kind": active_step.kind,
            "graphblocks.ai/rollout-traffic-percent": str(active_step.traffic_percent),
            "graphblocks.ai/stable-revision": rollout_plan.stable_revision_id,
        }
        service_labels = {
            **options.labels,
            "app.kubernetes.io/managed-by": "graphblocks",
            "app.kubernetes.io/name": name,
            "graphblocks.ai/rollout-id": rollout_plan.rollout_id,
        }
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": _object_metadata(name, options, service_labels, service_annotations),
                "spec": {
                    "type": "ClusterIP",
                    "selector": {key: service_selector[key] for key in sorted(service_selector)},
                    "ports": [port.service_contract() for port in ports],
                },
            }
        )

    return KubernetesManifestSet(tuple(documents))


def render_target_manifests(
    name: str,
    target: ExecutionTarget,
    *,
    options: KubernetesRenderOptions | None = None,
    replicas: int = 1,
    image: str | None = None,
    ports: Iterable[KubernetesPort] = (),
    env: Mapping[str, str] | Iterable[KubernetesEnv | KubernetesSecretEnv] = (),
) -> KubernetesManifestSet:
    options = options or KubernetesRenderOptions()
    ports = tuple(ports)
    documents = [
        render_target_deployment(
            name,
            target,
            options=options,
            replicas=replicas,
            image=image,
            ports=ports,
            env=env,
        )
    ]
    if ports:
        documents.append(render_target_service(name, target, ports, options=options))
    return KubernetesManifestSet(tuple(documents))


def render_callback_ingress_manifests(
    name: str,
    callback_ingress: Mapping[str, object],
    *,
    service_name: str,
    service_port: int = 8080,
    parent_refs: Iterable[Mapping[str, object]] = (),
    options: KubernetesRenderOptions | None = None,
    include_network_policy: bool = True,
) -> KubernetesManifestSet:
    if not name.strip():
        raise KubernetesAdapterError("callback ingress name must not be empty")
    if not service_name.strip():
        raise KubernetesAdapterError("callback ingress service_name must not be empty")
    if not 1 <= service_port <= 65535:
        raise KubernetesAdapterError("callback ingress service_port must be between 1 and 65535")
    options = options or KubernetesRenderOptions()
    config = _callback_ingress_contract(callback_ingress)
    if not config["enabled"]:
        return KubernetesManifestSet(())
    security = config["security"]
    if not security["require_signature"]:
        raise KubernetesAdapterError("GB6002: enabled callback ingress must require signatures")

    parent_refs = tuple(deepcopy(dict(parent_ref)) for parent_ref in parent_refs)
    if not parent_refs:
        raise KubernetesAdapterError("callback ingress HTTPRoute must declare at least one parentRef")

    labels = _callback_ingress_labels(name, options)
    annotations = _callback_ingress_annotations(config)
    metadata = _object_metadata(name, options, labels, annotations)
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": service_name,
            },
            "ports": [
                {
                    "name": "http",
                    "port": service_port,
                    "targetPort": service_port,
                    "protocol": "TCP",
                }
            ],
        },
    }
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": deepcopy(metadata),
        "spec": {
            "parentRefs": list(parent_refs),
            "rules": [
                {
                    "matches": [
                        {
                            "path": _callback_route_path_match(
                                route["path"],
                                allow_regex=options.gateway_regex_path_matches,
                            ),
                            "method": route["method"],
                        }
                    ],
                    "backendRefs": [
                        {
                            "name": name,
                            "port": service_port,
                        }
                    ],
                    "filters": [
                        {
                            "type": "RequestHeaderModifier",
                            "requestHeaderModifier": {
                                "set": [
                                    {
                                        "name": "GraphBlocks-Callback-Command",
                                        "value": route["command"],
                                    }
                                ]
                            },
                        }
                    ],
                }
                for route in config["routes"]
            ],
        },
    }
    documents = [service, route]
    if include_network_policy:
        documents.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": deepcopy(metadata),
                "spec": {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": service_name,
                        }
                    },
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "ports": [
                                {
                                    "protocol": "TCP",
                                    "port": service_port,
                                }
                            ]
                        }
                    ],
                },
            }
        )
    return KubernetesManifestSet(tuple(documents))


def render_helm_chart(
    chart_name: str,
    manifest_set: KubernetesManifestSet,
    *,
    chart_version: str = "0.1.0",
    app_version: str | None = None,
    description: str | None = None,
    values: Mapping[str, object] | None = None,
) -> HelmChartPackage:
    if not chart_name.strip():
        raise KubernetesAdapterError("Helm chart name must not be empty")
    if not chart_version.strip():
        raise KubernetesAdapterError("Helm chart version must not be empty")
    chart_document: dict[str, object] = {
        "apiVersion": "v2",
        "name": chart_name,
        "description": description or f"GraphBlocks deployment chart for {chart_name}",
        "type": "application",
        "version": chart_version,
    }
    if app_version is not None:
        chart_document["appVersion"] = str(app_version)

    chart_values = {str(key): deepcopy(value) for key, value in sorted(dict(values or {}).items())}
    try:
        _canonical_dumps(chart_values)
    except (TypeError, ValueError) as error:
        raise KubernetesAdapterError("Helm chart values must be JSON-serializable") from error

    used_paths = {"Chart.yaml", "values.yaml"}
    files = [
        HelmChartFile("Chart.yaml", _canonical_dumps(chart_document) + "\n"),
        HelmChartFile("values.yaml", _canonical_dumps(chart_values) + "\n"),
    ]
    for index, document in enumerate(manifest_set.documents, start=1):
        metadata = document.get("metadata")
        raw_name = metadata.get("name", f"document-{index:02d}") if isinstance(metadata, Mapping) else f"document-{index:02d}"
        path_segments: list[str] = []
        for raw_segment in (raw_name, document.get("kind", "manifest")):
            text = str(raw_segment).strip().lower()
            characters: list[str] = []
            previous_dash = False
            for character in text:
                if character.isalnum() or character in ".-":
                    characters.append(character)
                    previous_dash = False
                    continue
                if not previous_dash:
                    characters.append("-")
                    previous_dash = True
            segment = "".join(characters).strip(".-")
            while "--" in segment:
                segment = segment.replace("--", "-")
            if not segment:
                raise KubernetesAdapterError("Helm template name segment must not be empty")
            path_segments.append(segment)
        base_path = f"templates/{path_segments[0]}-{path_segments[1]}.yaml"
        path = base_path
        suffix = 2
        while path in used_paths:
            path = base_path.removesuffix(".yaml") + f"-{suffix}.yaml"
            suffix += 1
        used_paths.add(path)
        files.append(HelmChartFile(path, _canonical_dumps(document) + "\n"))
    return HelmChartPackage(chart_name, tuple(files))


def _callback_ingress_contract(callback_ingress: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(callback_ingress, Mapping):
        raise KubernetesAdapterError("callback ingress config must be a mapping")
    raw_enabled = callback_ingress.get("enabled", False)
    if not isinstance(raw_enabled, bool):
        raise KubernetesAdapterError("callback ingress enabled must be a boolean")
    enabled = raw_enabled
    raw_routes = callback_ingress.get("routes", ())
    if not isinstance(raw_routes, Iterable) or isinstance(raw_routes, (str, bytes, Mapping)):
        raise KubernetesAdapterError("callback ingress routes must be a list")
    routes: list[dict[str, str]] = []
    for raw_route in raw_routes:
        if not isinstance(raw_route, Mapping):
            raise KubernetesAdapterError("callback ingress route must be a mapping")
        path = raw_route.get("path")
        command = raw_route.get("command")
        method = raw_route.get("method", "POST")
        if not isinstance(path, str) or not path.strip():
            raise KubernetesAdapterError("callback ingress route path must not be empty")
        if not isinstance(command, str) or not command.strip():
            raise KubernetesAdapterError("callback ingress route command must not be empty")
        if not isinstance(method, str) or method.upper() not in {
            "DELETE",
            "GET",
            "HEAD",
            "OPTIONS",
            "PATCH",
            "POST",
            "PUT",
        }:
            raise KubernetesAdapterError("callback ingress route method must be a supported HTTP method")
        routes.append({"path": path, "command": command, "method": method.upper()})
    if enabled and not any(route["command"] == "SubmitAsyncCallback" for route in routes):
        raise KubernetesAdapterError("enabled callback ingress requires a SubmitAsyncCallback route")

    raw_security = callback_ingress.get("security", {})
    if not isinstance(raw_security, Mapping):
        raise KubernetesAdapterError("callback ingress security must be a mapping")
    security = {
        "require_signature": _bool_config(raw_security, "requireSignature", "require_signature", default=True),
        "anti_enumeration": _bool_config(raw_security, "antiEnumeration", "anti_enumeration", default=True),
    }
    raw_limits = callback_ingress.get("limits", {})
    if not isinstance(raw_limits, Mapping):
        raise KubernetesAdapterError("callback ingress limits must be a mapping")
    limits = {
        "max_payload_bytes": _positive_int_config(
            raw_limits,
            "maxPayloadBytes",
            "max_payload_bytes",
            default=262_144,
        ),
        "max_requests_per_second": _positive_int_config(
            raw_limits,
            "maxRequestsPerSecond",
            "max_requests_per_second",
            default=100,
        ),
    }
    return {
        "enabled": enabled,
        "routes": routes,
        "security": security,
        "limits": limits,
    }


def _bool_config(config: Mapping[str, object], camel: str, snake: str, *, default: bool) -> bool:
    value = config.get(camel, config.get(snake, default))
    if not isinstance(value, bool):
        raise KubernetesAdapterError(f"callback ingress {camel} must be a boolean")
    return value


def _positive_int_config(config: Mapping[str, object], camel: str, snake: str, *, default: int) -> int:
    value = config.get(camel, config.get(snake, default))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KubernetesAdapterError(f"callback ingress {camel} must be a positive integer")
    return value


def _callback_ingress_labels(name: str, options: KubernetesRenderOptions) -> dict[str, str]:
    labels = {
        **options.labels,
        "app.kubernetes.io/component": "callback-gateway",
        "app.kubernetes.io/managed-by": "graphblocks",
        "app.kubernetes.io/name": name,
    }
    return {key: labels[key] for key in sorted(labels)}


def _callback_ingress_annotations(config: Mapping[str, object]) -> dict[str, str]:
    security = config["security"]
    limits = config["limits"]
    if not isinstance(security, Mapping) or not isinstance(limits, Mapping):
        raise KubernetesAdapterError("callback ingress config is malformed")
    annotations = {
        "graphblocks.ai/anti-enumeration": str(security["anti_enumeration"]).lower(),
        "graphblocks.ai/callback-ingress": "true",
        "graphblocks.ai/max-payload-bytes": str(limits["max_payload_bytes"]),
        "graphblocks.ai/max-requests-per-second": str(limits["max_requests_per_second"]),
        "graphblocks.ai/require-signature": str(security["require_signature"]).lower(),
    }
    return {key: annotations[key] for key in sorted(annotations)}


def _callback_route_path_match(path: str, *, allow_regex: bool) -> dict[str, str]:
    if "{" not in path and "}" not in path:
        return {"type": "Exact", "value": path}
    first_placeholder = re.search(r"\{([^/{ }]+)\}", path)
    if first_placeholder is None:
        raise KubernetesAdapterError(
            "callback ingress route path contains a malformed placeholder"
        )
    if first_placeholder.end() == len(path):
        return {"type": "PathPrefix", "value": path[: first_placeholder.start()]}
    if not allow_regex:
        raise KubernetesAdapterError(
            "callback ingress routes with path segments after a placeholder require "
            "gateway_regex_path_matches"
        )
    pattern: list[str] = ["^"]
    cursor = 0
    for match in re.finditer(r"\{([^/{ }]+)\}", path):
        pattern.append(re.escape(path[cursor : match.start()]))
        pattern.append("[^/]+")
        cursor = match.end()
    pattern.append(re.escape(path[cursor:]))
    pattern.append("$")
    rendered = "".join(pattern)
    if "{" in rendered or "}" in rendered:
        raise KubernetesAdapterError(
            "callback ingress route path contains a malformed placeholder"
        )
    return {"type": "RegularExpression", "value": rendered}


__all__ = [
    "HelmChartFile",
    "HelmChartPackage",
    "KubernetesAdapterError",
    "KubernetesClusterCapability",
    "KubernetesClusterSnapshot",
    "KubernetesEnv",
    "KubernetesManifest",
    "KubernetesManifestSet",
    "KubernetesPort",
    "KubernetesProtocol",
    "KubernetesRenderOptions",
    "KubernetesSecretEnv",
    "render_callback_ingress_manifests",
    "render_rollout_manifests",
    "render_helm_chart",
    "render_target_deployment",
    "render_target_manifests",
    "render_target_service",
]
