from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess

from jsonschema import Draft7Validator
import pytest
import yaml

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]
CHART_ROOT = ROOT / "packages" / "graphblocks-deployment-chart"


def _render_chart(
    values: dict[str, object] | None, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    helm = os.environ.get("GRAPHBLOCKS_HELM") or shutil.which("helm")
    if helm is None:
        if os.environ.get("GRAPHBLOCKS_REQUIRE_HELM") == "1":
            pytest.fail("GRAPHBLOCKS_REQUIRE_HELM=1 but helm is not installed")
        pytest.skip("helm is not installed")

    command = [helm, "template", "audit", str(CHART_ROOT)]
    if values is not None:
        values_path = tmp_path / "values.yaml"
        values_path.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")
        command.extend(["--values", str(values_path)])
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _rendered_documents(output: str) -> list[dict[str, object]]:
    return [
        document
        for document in yaml.safe_load_all(output)
        if isinstance(document, dict)
    ]


def test_deployment_chart_catalog_entry_is_a_non_implementing_helm_scaffold() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert "graphblocks-operator" not in rows
    assert rows["graphblocks-deployment-chart"] == {
        "component": "graphblocks-deployment-chart",
        "artifact": "graphblocks-deployment-chart",
        "distribution": "graphblocks-deployment-chart",
        "import": None,
        "default": False,
        "layer": "deployment_scaffold",
        "kind": "helm_scaffold",
        "implementationPhase": 4,
        "stability": "first-party-extension",
    }
    assert not (ROOT / "packages" / "graphblocks-operator").exists()
    assert not (CHART_ROOT / "pyproject.toml").exists()
    assert not (CHART_ROOT / "src").exists()
    assert not any(CHART_ROOT.rglob("Dockerfile*"))


def test_deployment_chart_metadata_and_defaults_make_no_controller_claim() -> None:
    chart = yaml.safe_load((CHART_ROOT / "Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))

    assert chart == {
        "apiVersion": "v2",
        "name": "graphblocks-deployment-chart",
        "description": (
            "Internal disabled-by-default Helm scaffold for a user-supplied "
            "GraphBlocks Kubernetes controller; no controller implementation is included"
        ),
        "type": "application",
        "version": "0.1.0",
        "appVersion": "0.1.0",
        "annotations": {
            "graphblocks.ai/artifact-kind": "deployment-scaffold",
            "graphblocks.ai/controller-included": "false",
            "graphblocks.ai/maturity": "internal",
        },
    }
    assert values["image"] == {
        "repository": "",
        "tag": "",
        "digest": "",
        "pullPolicy": "IfNotPresent",
    }
    assert values["scaffold"] == {
        "enabled": False,
        "releaseId": "graphblocks-deployment-chart",
        "accessNamespaces": [],
        "replicas": 1,
        "args": [],
        "env": [],
    }
    assert values["serviceAccount"] == {"create": False, "name": ""}
    assert values["rbac"] == {
        "create": False,
        "clusterWide": False,
        "rules": [],
    }


def test_deployment_chart_values_schema_is_closed_and_fail_closed() -> None:
    schema = json.loads((CHART_ROOT / "values.schema.json").read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    defaults = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))

    assert not list(validator.iter_errors(defaults))

    unknown = deepcopy(defaults)
    unknown["unknown"] = True
    assert list(validator.iter_errors(unknown))

    string_false = deepcopy(defaults)
    string_false["scaffold"]["enabled"] = "false"
    assert list(validator.iter_errors(string_false))

    enabled_without_image = deepcopy(defaults)
    enabled_without_image["scaffold"]["enabled"] = True
    assert list(validator.iter_errors(enabled_without_image))

    enabled = deepcopy(enabled_without_image)
    enabled["image"]["repository"] = "registry.example:5000/team/controller"
    enabled["image"]["tag"] = "test"
    assert not list(validator.iter_errors(enabled))

    unsafe_repository = deepcopy(enabled)
    unsafe_repository["image"]["repository"] = (
        'registry.example/controller"\nsecurityContext: {}'
    )
    assert list(validator.iter_errors(unsafe_repository))

    unsafe_tag = deepcopy(enabled)
    unsafe_tag["image"]["tag"] = 'test"\nsecurityContext: {}'
    assert list(validator.iter_errors(unsafe_tag))

    ambiguous_image = deepcopy(enabled)
    ambiguous_image["image"]["digest"] = "sha256:" + ("a" * 64)
    assert list(validator.iter_errors(ambiguous_image))

    reserved_label = deepcopy(enabled)
    reserved_label["podLabels"]["app.kubernetes.io/name"] = "override"
    assert list(validator.iter_errors(reserved_label))

    reserved_annotation = deepcopy(enabled)
    reserved_annotation["podAnnotations"][
        "graphblocks.ai/deployment-scaffold-release-id"
    ] = "override"
    assert list(validator.iter_errors(reserved_annotation))

    rbac_without_identity_or_rules = deepcopy(enabled)
    rbac_without_identity_or_rules["rbac"]["create"] = True
    assert list(validator.iter_errors(rbac_without_identity_or_rules))

    explicit_rbac = deepcopy(enabled)
    explicit_rbac["serviceAccount"]["name"] = "existing-controller"
    explicit_rbac["rbac"] = {
        "create": True,
        "clusterWide": False,
        "rules": [
            {
                "apiGroups": ["graphblocks.ai"],
                "resources": ["graphdeployments"],
                "verbs": ["get", "list", "watch"],
            }
        ],
    }
    assert not list(validator.iter_errors(explicit_rbac))

    cluster_without_rbac = deepcopy(enabled)
    cluster_without_rbac["rbac"]["clusterWide"] = True
    assert list(validator.iter_errors(cluster_without_rbac))


def test_deployment_chart_templates_remove_fake_operator_defaults_and_permissions() -> (
    None
):
    templates = {
        path.name: path.read_text(encoding="utf-8")
        for path in (CHART_ROOT / "templates").iterdir()
    }
    combined = "\n".join(templates.values())

    assert templates["deployment.yaml"].startswith("{{- if .Values.scaffold.enabled }}")
    assert templates["serviceaccount.yaml"].startswith(
        "{{- if and .Values.scaffold.enabled .Values.serviceAccount.create }}"
    )
    assert templates["rbac.yaml"].startswith(
        "{{- if and .Values.scaffold.enabled .Values.rbac.create }}"
    )
    assert (
        "this chart does not include a controller image" in templates["deployment.yaml"]
    )
    assert (
        "serviceAccount.create=true or serviceAccount.name is required"
        in (templates["deployment.yaml"])
    )
    assert "toYaml .Values.rbac.rules" in templates["rbac.yaml"]
    assert "toYaml $root.Values.rbac.rules" in templates["rbac.yaml"]
    assert "image: {{ $imageReference | quote }}" in templates["deployment.yaml"]
    assert (
        'omit .Values.podLabels "app.kubernetes.io/name" '
        '"app.kubernetes.io/component" "app.kubernetes.io/instance"'
        in templates["deployment.yaml"]
    )
    assert (
        'omit .Values.podAnnotations '
        '"graphblocks.ai/deployment-scaffold-release-id"'
        in templates["deployment.yaml"]
    )

    for forbidden in (
        "graphblocks-operator",
        "ghcr.io/graphblocks",
        "--watch-graphdeployments",
        "--watch-graphreleases",
        "GRAPHBLOCKS_OPERATOR_RELEASE_ID",
        "graphreleases",
        "graphdeployments",
        "deploymentrevisions",
    ):
        assert forbidden not in combined


def test_deployment_chart_defaults_render_no_resources(tmp_path: Path) -> None:
    rendered = _render_chart(None, tmp_path)
    assert rendered.returncode == 0, rendered.stderr
    assert _rendered_documents(rendered.stdout) == []


def test_deployment_chart_enabled_without_image_fails_render(tmp_path: Path) -> None:
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    values["scaffold"]["enabled"] = True

    rendered = _render_chart(values, tmp_path)
    assert rendered.returncode != 0
    assert "repository" in rendered.stderr


def test_deployment_chart_rejects_unsafe_image_and_reserved_metadata(
    tmp_path: Path,
) -> None:
    defaults = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    defaults["scaffold"]["enabled"] = True
    defaults["image"]["repository"] = "registry.example/controller"
    defaults["image"]["tag"] = "test"
    overrides = (
        {
            "image": {
                "repository": 'registry.example/controller"\nsecurityContext: {}'
            }
        },
        {"image": {"tag": 'test"\nsecurityContext: {}'}},
        {"image": {"digest": "sha256:" + ("a" * 64)}},
        {"podLabels": {"app.kubernetes.io/name": "override"}},
        {
            "podAnnotations": {
                "graphblocks.ai/deployment-scaffold-release-id": "override"
            }
        },
    )

    for override in overrides:
        values = deepcopy(defaults)
        for section, fields in override.items():
            values[section].update(fields)
        rendered = _render_chart(values, tmp_path)
        assert rendered.returncode != 0, override


def test_deployment_chart_enabled_with_image_renders_only_workload(
    tmp_path: Path,
) -> None:
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    values["scaffold"]["enabled"] = True
    values["image"]["repository"] = "registry.example/controller"
    values["image"]["tag"] = "test"

    rendered = _render_chart(values, tmp_path)
    assert rendered.returncode == 0, rendered.stderr
    documents = _rendered_documents(rendered.stdout)
    assert [document["kind"] for document in documents] == ["Deployment"]
    deployment = documents[0]
    assert deployment["metadata"]["labels"]["app.kubernetes.io/component"] == (
        "controller-scaffold"
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    assert "serviceAccountName" not in pod_spec
    assert pod_spec["containers"][0]["image"] == "registry.example/controller:test"
    assert "graphblocks-operator" not in rendered.stdout
    assert "--watch-graphdeployments" not in rendered.stdout


def test_deployment_chart_rbac_is_explicit_namespaced_input(tmp_path: Path) -> None:
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    values["scaffold"]["enabled"] = True
    values["scaffold"]["accessNamespaces"] = ["team-a", "team-b"]
    values["image"]["repository"] = "registry.example/controller"
    values["image"]["digest"] = "sha256:" + ("a" * 64)
    values["serviceAccount"] = {"create": True, "name": ""}
    values["rbac"] = {
        "create": True,
        "clusterWide": False,
        "rules": [
            {
                "apiGroups": ["graphblocks.ai"],
                "resources": ["graphdeployments"],
                "verbs": ["get", "list", "watch"],
            }
        ],
    }

    rendered = _render_chart(values, tmp_path)
    assert rendered.returncode == 0, rendered.stderr
    documents = _rendered_documents(rendered.stdout)
    kinds = [document["kind"] for document in documents]
    assert kinds.count("Deployment") == 1
    assert kinds.count("ServiceAccount") == 1
    assert kinds.count("Role") == 2
    assert kinds.count("RoleBinding") == 2
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    roles = [document for document in documents if document["kind"] == "Role"]
    assert {role["metadata"]["namespace"] for role in roles} == {"team-a", "team-b"}
    assert all(role["rules"] == values["rbac"]["rules"] for role in roles)


def test_deployment_chart_cluster_rbac_requires_explicit_opt_in(tmp_path: Path) -> None:
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))
    values["scaffold"]["enabled"] = True
    values["image"]["repository"] = "registry.example/controller"
    values["image"]["tag"] = "test"
    values["serviceAccount"] = {"create": False, "name": "existing-controller"}
    values["rbac"] = {
        "create": True,
        "clusterWide": True,
        "rules": [
            {
                "apiGroups": ["graphblocks.ai"],
                "resources": ["graphdeployments"],
                "verbs": ["get", "list", "watch"],
            }
        ],
    }

    rendered = _render_chart(values, tmp_path)
    assert rendered.returncode == 0, rendered.stderr
    documents = _rendered_documents(rendered.stdout)
    kinds = [document["kind"] for document in documents]
    assert len(kinds) == 3
    assert set(kinds) == {"Deployment", "ClusterRole", "ClusterRoleBinding"}
    deployment = next(
        document for document in documents if document["kind"] == "Deployment"
    )
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == (
        "existing-controller"
    )
