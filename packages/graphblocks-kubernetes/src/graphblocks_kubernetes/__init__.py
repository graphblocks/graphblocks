from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Literal

from graphblocks_deployment import ExecutionTarget


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


@dataclass(frozen=True, slots=True)
class KubernetesRenderOptions:
    namespace: str = "default"
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)
    service_account_name: str | None = None
    image_pull_secrets: tuple[str, ...] = field(default_factory=tuple)
    runtime_class_name: str | None = None

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise KubernetesAdapterError("namespace must not be empty")
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
    env: Mapping[str, str] | Iterable[KubernetesEnv] = (),
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
    if isinstance(env, Mapping):
        env_contracts = [
            KubernetesEnv(str(key), str(value)).env_contract()
            for key, value in sorted(env.items())
        ]
    else:
        env_contracts = [
            item.env_contract()
            for item in sorted(tuple(env), key=lambda env_item: env_item.name)
        ]

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


def render_target_manifests(
    name: str,
    target: ExecutionTarget,
    *,
    options: KubernetesRenderOptions | None = None,
    replicas: int = 1,
    image: str | None = None,
    ports: Iterable[KubernetesPort] = (),
    env: Mapping[str, str] | Iterable[KubernetesEnv] = (),
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


__all__ = [
    "KubernetesAdapterError",
    "KubernetesClusterCapability",
    "KubernetesClusterSnapshot",
    "KubernetesEnv",
    "KubernetesManifest",
    "KubernetesManifestSet",
    "KubernetesPort",
    "KubernetesProtocol",
    "KubernetesRenderOptions",
    "render_target_deployment",
    "render_target_manifests",
    "render_target_service",
]
