from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graphblocks.deployment import DeploymentTargetProfile, DeploymentTargetProfileSet, GraphDeploymentError


ROOT = Path(__file__).parents[1]
REQUIRED_PRODUCTION_IMAGE_ROLES = (
    "control-plane",
    "rag-cpu",
    "document-cpu",
    "ocr-gpu",
    "sandbox",
)


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_production_target_manifest_covers_phase_five_image_roles() -> None:
    target_set = DeploymentTargetProfileSet.from_document(
        _load_yaml(ROOT / "deployment" / "production-targets.yaml")
    )

    coverage = target_set.coverage_for_required_image_roles(REQUIRED_PRODUCTION_IMAGE_ROLES)

    assert coverage.ok
    assert coverage.issue_contracts() == []
    assert target_set.image_roles() == REQUIRED_PRODUCTION_IMAGE_ROLES
    assert target_set.target_ids() == ("control", "document-cpu", "ocr-gpu", "rag-cpu", "sandbox")


def test_production_target_profiles_project_to_execution_targets() -> None:
    target_set = DeploymentTargetProfileSet.from_document(
        _load_yaml(ROOT / "deployment" / "production-targets.yaml")
    )
    control = target_set.by_id("control")

    target = control.to_execution_target("registry.example.com/gb/control@sha256:control")

    assert control.profile_contract() == {
        "target_id": "control",
        "image_role": "control-plane",
        "kind": "service",
        "execution_host": "rust",
        "capabilities": ["graph.coordinator", "model.remote_call", "retrieval.remote_call"],
        "effects": ["network"],
        "package_lock": "locks/control.lock",
        "default_replicas": 2,
    }
    assert target.canonical_value() == {
        "target_id": "control",
        "kind": "service",
        "execution_host": "rust",
        "capabilities": ["graph.coordinator", "model.remote_call", "retrieval.remote_call"],
        "effects": ["network"],
        "package_lock": "locks/control.lock",
        "image": "registry.example.com/gb/control@sha256:control",
    }
    assert target_set.content_digest().startswith("sha256:")


def test_deployment_target_profile_rejects_invalid_string_fields() -> None:
    with pytest.raises(GraphDeploymentError, match="deployment target profile id must be a string"):
        DeploymentTargetProfile(
            target_id=object(),  # type: ignore[arg-type]
            image_role="control-plane",
            kind="service",
            execution_host="rust",
        )
    with pytest.raises(GraphDeploymentError, match="deployment target image_role must be a string"):
        DeploymentTargetProfile(
            target_id="control",
            image_role=object(),  # type: ignore[arg-type]
            kind="service",
            execution_host="rust",
        )
    with pytest.raises(GraphDeploymentError, match="deployment target execution_host must be a string"):
        DeploymentTargetProfile(
            target_id="control",
            image_role="control-plane",
            kind="service",
            execution_host=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(GraphDeploymentError, match="deployment target kind"):
        DeploymentTargetProfile(
            target_id="control",
            image_role="control-plane",
            kind="unknown",  # type: ignore[arg-type]
            execution_host="rust",
        )


def test_deployment_target_profile_rejects_unstable_capability_sets() -> None:
    for field_name, value in (
        ("capabilities", "graph.coordinator"),
        ("capabilities", (" ",)),
        ("capabilities", (object(),)),
        ("effects", "network"),
        ("effects", (" ",)),
    ):
        with pytest.raises(GraphDeploymentError, match=field_name):
            DeploymentTargetProfile(
                target_id="control",
                image_role="control-plane",
                kind="service",
                execution_host="rust",
                **{field_name: value},
            )


def test_deployment_target_profile_mapping_rejects_non_collection_capabilities() -> None:
    with pytest.raises(GraphDeploymentError, match="capabilities must be a collection"):
        DeploymentTargetProfile.from_mapping(
            {
                "id": "control",
                "imageRole": "control-plane",
                "kind": "service",
                "executionHost": "rust",
                "capabilities": 7,
            }
        )


@pytest.mark.parametrize("default_replicas", [True, False])
def test_deployment_target_profile_rejects_boolean_default_replicas(
    default_replicas: bool,
) -> None:
    with pytest.raises(
        GraphDeploymentError,
        match="deployment target profile defaultReplicas must be an integer",
    ):
        DeploymentTargetProfile.from_mapping(
            {
                "id": "control",
                "imageRole": "control-plane",
                "kind": "service",
                "executionHost": "rust",
                "defaultReplicas": default_replicas,
            }
        )


def test_deployment_target_coverage_reports_missing_image_role() -> None:
    target_set = DeploymentTargetProfileSet(())

    coverage = target_set.coverage_for_required_image_roles(("control-plane",))

    assert not coverage.ok
    assert coverage.issue_contracts() == [
        {
            "code": "DeploymentTargetRoleMissing",
            "image_role": "control-plane",
            "target_id": "",
            "path": "$.spec.targets",
            "message": "required production image role has no deployment target profile",
        }
    ]
