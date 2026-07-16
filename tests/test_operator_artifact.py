from __future__ import annotations

from pathlib import Path

import yaml

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]
OPERATOR_ROOT = ROOT / "packages" / "graphblocks-operator"


def test_operator_catalog_entry_is_helm_oci_artifact_not_python_package() -> None:
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-operator"] == {
        "component": "graphblocks-operator",
        "artifact": "graphblocks-operator",
        "distribution": "graphblocks-operator",
        "import": None,
        "default": False,
        "layer": "platform_controller",
        "kind": "oci_image_and_helm",
        "implementationPhase": 4,
        "stability": "first-party-extension",
    }
    assert not (OPERATOR_ROOT / "pyproject.toml").exists()
    assert not (OPERATOR_ROOT / "src").exists()


def test_operator_chart_has_default_controller_image_and_release_identity() -> None:
    chart = yaml.safe_load((OPERATOR_ROOT / "Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((OPERATOR_ROOT / "values.yaml").read_text(encoding="utf-8"))

    assert chart == {
        "apiVersion": "v2",
        "name": "graphblocks-operator",
        "description": "GraphBlocks Kubernetes reconciliation controller",
        "type": "application",
        "version": "0.1.0",
        "appVersion": "0.1.0",
    }
    assert values["image"] == {
        "repository": "ghcr.io/graphblocks/graphblocks-operator",
        "tag": "0.1.0",
        "digest": "",
        "pullPolicy": "IfNotPresent",
    }
    assert values["controller"]["releaseId"] == "graphblocks-operator"
    assert values["controller"]["watchNamespaces"] == []
    assert values["serviceAccount"]["create"] is True
    assert values["serviceAccount"]["name"] == ""
    assert values["rbac"]["create"] is True
    assert values["rbac"]["clusterWide"] is False


def test_operator_chart_templates_controller_deployment_and_rbac() -> None:
    deployment = (OPERATOR_ROOT / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    service_account = (OPERATOR_ROOT / "templates" / "serviceaccount.yaml").read_text(encoding="utf-8")
    rbac = (OPERATOR_ROOT / "templates" / "rbac.yaml").read_text(encoding="utf-8")
    helpers = (OPERATOR_ROOT / "templates" / "_helpers.tpl").read_text(encoding="utf-8")

    assert "kind: Deployment" in deployment
    assert "app.kubernetes.io/name: graphblocks-operator" in deployment
    assert 'include "graphblocks-operator.controllerName"' in deployment
    assert "GRAPHBLOCKS_OPERATOR_RELEASE_ID" in deployment
    assert "--watch-graphdeployments=true" in deployment
    assert "image: \"{{ .Values.image.repository }}" in deployment
    assert "kind: ServiceAccount" in service_account
    assert 'include "graphblocks-operator.serviceAccountName"' in service_account
    assert "kind: ClusterRole" in rbac
    assert "kind: Role" in rbac
    assert "graphdeployments" in helpers
    assert "graphreleases" in helpers
    assert "deploymentrevisions" in helpers
    assert "kind: ClusterRoleBinding" in rbac
    assert "kind: RoleBinding" in rbac
    assert 'printf "%s-controller"' in helpers
    assert "| trunc 52" in helpers
    assert 'printf "%s-%s-%s" .Release.Namespace .Release.Name .Chart.Name' in helpers


def test_operator_chart_uses_configured_service_account_name_consistently() -> None:
    deployment = (OPERATOR_ROOT / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    service_account = (OPERATOR_ROOT / "templates" / "serviceaccount.yaml").read_text(encoding="utf-8")
    rbac = (OPERATOR_ROOT / "templates" / "rbac.yaml").read_text(encoding="utf-8")

    value_expression = '{{ include "graphblocks-operator.serviceAccountName" . | quote }}'
    assert f"serviceAccountName: {value_expression}" in deployment
    assert f"name: {value_expression}" in service_account
    assert f"name: {value_expression}" in rbac
