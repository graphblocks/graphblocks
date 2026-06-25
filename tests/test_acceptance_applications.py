from __future__ import annotations

import importlib
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def _import_testing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    return importlib.import_module("graphblocks_testing")


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_acceptance_manifest_covers_conformance_profile_applications(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    conformance = _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")

    coverage = manifest.coverage_for_conformance(conformance, root=ROOT)

    assert coverage.ok
    assert coverage.issue_contracts() == []
    assert manifest.application_ids() == (
        "direct-file-analysis",
        "document-ingestion",
        "enterprise-rag",
        "kubernetes-canary",
        "multi-turn-chat",
        "telemetry-outage-correctness",
    )


def test_acceptance_manifest_entries_are_stable_contracts(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    enterprise_rag = manifest.by_id("enterprise-rag")

    assert enterprise_rag.application_contract() == {
        "application_id": "enterprise-rag",
        "profiles": ["GB-C2-AI-APPLICATION"],
        "scenario_path": "docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml",
        "gates": [
            "graphblocks validate",
            "graphblocks plan --expand",
            "rag citation validation",
            "abstention check",
        ],
        "description": "Federated enterprise RAG with dense and keyword retrieval, fusion, rerank, budgeted context, abstention, and citation checks.",
    }
    assert manifest.content_digest().startswith("sha256:")


def test_acceptance_manifest_reports_missing_profile_application(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        {
            "apiVersion": "graphblocks.ai/acceptance/v1alpha1",
            "kind": "AcceptanceApplicationSet",
            "spec": {"applications": []},
        }
    )
    conformance = {
        "spec": {
            "profiles": [
                {
                    "id": "GB-C2-AI-APPLICATION",
                    "acceptanceApplications": ["enterprise-rag"],
                }
            ]
        }
    }

    coverage = manifest.coverage_for_conformance(conformance, root=ROOT)

    assert not coverage.ok
    assert coverage.issue_contracts() == [
        {
            "code": "AcceptanceApplicationMissing",
            "application_id": "enterprise-rag",
            "profile_id": "GB-C2-AI-APPLICATION",
            "path": "$.spec.profiles[0].acceptanceApplications[0]",
            "message": "profile references an acceptance application with no manifest entry",
        }
    ]
