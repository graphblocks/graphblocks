from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]


def _import_testing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    return importlib.import_module("graphblocks_testing")


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_yaml_documents(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document is not None]


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
        "bounded-research-orchestrator",
        "coding-agent-background-callbacks",
        "direct-file-analysis",
        "document-ingestion",
        "enterprise-rag",
        "kubernetes-canary",
        "multi-turn-chat",
        "realtime-voice-agent",
        "telemetry-outage-correctness",
        "verified-rtl-workspace-trial",
    )


def test_profiled_examples_are_declared_acceptance_applications(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )

    scenario_paths = {
        application.scenario_path for application in manifest.applications
    }

    assert scenario_paths >= {
        "docs/upstream/GraphBlocks_v1.0_Final/examples/01-enterprise-federated-rag.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/02-document-ingestion.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/03-policy-governed-chat.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/05-authority-backed-advisory.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/06-bounded-research-orchestrator.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/07-verified-rtl-workspace-trial.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/08-kubernetes-production-deployment.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/09-observability-profile.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/10-realtime-voice-extension.yaml",
        "docs/upstream/GraphBlocks_v1.0_Final/examples/11-coding-agent-background-callbacks.yaml",
    }


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


def test_coding_agent_background_callback_example_matches_async_contract() -> None:
    application, graph, callback = _load_yaml_documents(
        ROOT
        / "docs"
        / "upstream"
        / "GraphBlocks_v1.0_Final"
        / "examples"
        / "11-coding-agent-background-callbacks.yaml"
    )

    assert application["kind"] == "Application"
    assert application["metadata"]["name"] == "workspace-coding-agent"
    assert set(application["spec"]["capabilities"]) >= {
        "background_runs",
        "cursor_replay",
        "callback_subscription",
        "reconnect_resume",
    }
    routes = {route["id"]: route for route in application["spec"]["routes"]}
    assert routes["create-task"]["responseMode"] == "accepted"
    assert routes["run-events"] == {
        "id": "run-events",
        "method": "GET",
        "path": "/v1/runs/{run_id}/events",
        "transport": "sse",
        "cursorReplay": True,
    }
    assert routes["external-callback"] == {
        "id": "external-callback",
        "method": "POST",
        "path": "/v1/callbacks/{operation_id}",
        "command": "SubmitAsyncCallback",
    }

    assert graph["kind"] == "Graph"
    assert graph["metadata"]["name"] == "coding-agent-task"
    assert graph["spec"]["execution"] == {
        "lifetime": "job",
        "durability": "checkpointed",
        "interaction": "incremental",
    }
    nodes = graph["spec"]["nodes"]
    assert nodes["startCI"]["block"] == "async.start_operation@1"
    assert nodes["startCI"]["config"]["callback"] == {
        "required": True,
        "schema": "schemas/CICallback@1",
        "preCommitRace": {
            "onEarlyCallback": "quarantine",
            "quarantineTtl": "5m",
            "onQuarantineExpired": "reject_without_resume",
            "idempotencyKey": "provider_delivery_id",
        },
    }
    assert nodes["startCI"]["config"]["timeout"] == "30m"
    assert nodes["waitCI"] == {
        "block": "async.await_callback@1",
        "inputs": {"operation": "startCI.operation"},
        "config": {"checkpoint": True, "onTimeout": "fail"},
    }
    assert nodes["commit"]["config"] == {
        "concurrency": "compare_and_swap",
        "requireCleanBase": True,
    }

    assert callback["type"] == "RegisterCallback"
    assert callback["scope"] == "run"
    assert callback["event_filter"]["types"] == [
        "ReviewRequested",
        "ApprovalRequested",
        "BudgetExhausted",
        "RunCompleted",
        "RunFailed",
    ]
    assert callback["delivery"]["kind"] == "webhook"
    assert callback["delivery"]["signing"]["algorithm"] == "hmac-sha256"
    assert callback["delivery"]["retry_policy_ref"] == "webhook-standard"


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


def test_conformance_profile_set_resolves_inherited_tck_and_acceptance_requirements(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    claim = profile_set.claim_requirements(("GB-C2-AI-APPLICATION",))

    assert claim.profile_ids == ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME", "GB-C2-AI-APPLICATION")
    assert claim.tck_suites == (
        "application-events",
        "application-protocol",
        "compiler",
        "conversation",
        "documents",
        "rag",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
    )
    assert claim.acceptance_applications == (
        "direct-file-analysis",
        "document-ingestion",
        "enterprise-rag",
        "multi-turn-chat",
    )
    assert "ConformanceProfileSet" in graphblocks_testing.__all__


def test_conformance_profile_set_rejects_malformed_profile_lists(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    document = {
        "kind": "ConformanceProfileSet",
        "spec": {
            "profiles": [
                {
                    "id": "GB-C0-SCHEMA",
                    "status": "stable",
                    "tck": {"compiler": True},
                }
            ]
        },
    }

    with pytest.raises(ValueError, match=r"conformance profile 0 tck must be a list of strings"):
        graphblocks_testing.ConformanceProfileSet.from_document(document)


def test_upstream_conformance_profile_catalog_matches_shipped_catalog() -> None:
    assert _load_yaml(
        ROOT / "docs" / "upstream" / "GraphBlocks_v1.0_Final" / "catalog" / "conformance-profiles.yaml"
    ) == _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")


def test_conformance_profile_tck_suites_have_shared_fixture_manifests(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-C3-GOVERNED-RUNTIME",),
        graphblocks_testing.load_tck_suite_manifests(ROOT / "tck"),
    )

    assert coverage.ok
    assert coverage.claim.tck_suites == (
        "application-events",
        "approval-review",
        "budget-race",
        "compiler",
        "exhaustion",
        "policy",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
        "usage",
    )
    assert coverage.available_suites == (
        "application-events",
        "application-protocol",
        "approval-review",
        "budget-race",
        "compiler",
        "conversation",
        "deployment",
        "documents",
        "durable",
        "exhaustion",
        "orchestration",
        "policy",
        "rag",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
        "usage",
        "voice",
    )
    assert coverage.missing_suites == ()
    assert coverage.issue_contracts() == []
    assert coverage.coverage_contract()["ok"] is True
    assert coverage.content_digest().startswith("sha256:")
    assert "TckSuiteCoverageResult" in graphblocks_testing.__all__
    assert "check_tck_suite_coverage" in graphblocks_testing.__all__


def test_c4_conformance_profile_includes_deployment_tck_coverage(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-C4-PRODUCTION",),
        graphblocks_testing.load_tck_suite_manifests(ROOT / "tck"),
    )

    assert coverage.ok
    assert "deployment" in coverage.claim.tck_suites
    assert "durable" in coverage.claim.tck_suites
    assert "coding-agent-background-callbacks" in coverage.claim.acceptance_applications
    assert coverage.missing_suites == ()


def test_x1_conformance_profile_includes_orchestration_tck_coverage(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-X1-ORCHESTRATION",),
        graphblocks_testing.load_tck_suite_manifests(ROOT / "tck"),
    )

    assert coverage.ok
    assert "orchestration" in coverage.claim.tck_suites
    assert "bounded-research-orchestrator" in coverage.claim.acceptance_applications
    assert coverage.missing_suites == ()


def test_x2_conformance_profile_includes_voice_tck_coverage(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-X2-VOICE",),
        graphblocks_testing.load_tck_suite_manifests(ROOT / "tck"),
    )

    assert coverage.ok
    assert "voice" in coverage.claim.tck_suites
    assert "realtime-voice-agent" in coverage.claim.acceptance_applications
    assert coverage.missing_suites == ()


def test_x3_conformance_profile_includes_durable_tck_coverage(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-X3-DURABLE-STREAM",),
        graphblocks_testing.load_tck_suite_manifests(ROOT / "tck"),
    )

    assert coverage.ok
    assert "durable" in coverage.claim.tck_suites
    assert coverage.missing_suites == ()


def test_conformance_profile_tck_suite_coverage_reports_missing_fixtures(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    manifests = tuple(
        manifest
        for manifest in graphblocks_testing.load_tck_suite_manifests(ROOT / "tck")
        if manifest.suite_id not in {"exhaustion", "policy"}
    )

    coverage = graphblocks_testing.check_tck_suite_coverage(
        profile_set,
        ("GB-C3-GOVERNED-RUNTIME",),
        manifests,
    )

    assert not coverage.ok
    assert coverage.missing_suites == ("exhaustion", "policy")
    assert coverage.issue_contracts() == [
        {
            "code": "TckSuiteFixtureMissing",
            "profile_id": "GB-C3-GOVERNED-RUNTIME",
            "suite": "exhaustion",
            "path": "$.profiles.GB-C3-GOVERNED-RUNTIME.tck.exhaustion",
            "message": "conformance profile requires a TCK suite with no shared fixture manifest",
        },
        {
            "code": "TckSuiteFixtureMissing",
            "profile_id": "GB-C3-GOVERNED-RUNTIME",
            "suite": "policy",
            "path": "$.profiles.GB-C3-GOVERNED-RUNTIME.tck.policy",
            "message": "conformance profile requires a TCK suite with no shared fixture manifest",
        },
    ]


def test_conformance_profile_claim_validates_tck_and_acceptance_evidence(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    acceptance_coverage = manifest.coverage_for_conformance(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"),
        root=ROOT,
    )
    passing_reports = {
        suite: graphblocks_testing.TckReport(
            profile=suite,
            results=(graphblocks_testing.TckResult(suite, "compiler", "passed"),),
        )
        for suite in (
            "application-events",
            "application-protocol",
            "compiler",
            "conversation",
            "documents",
            "rag",
            "retry",
            "runtime",
            "schema",
            "sequence",
            "tool-execution",
            "tool-lifecycle",
            "tool-result",
        )
    }

    validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports=passing_reports,
        acceptance_coverage=acceptance_coverage,
    )

    assert validation.ok
    assert validation.issue_contracts() == []
    assert validation.claim.profile_ids == ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME", "GB-C2-AI-APPLICATION")


def test_conformance_profile_claim_reports_missing_inherited_tck(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )

    validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports={
            "compiler": graphblocks_testing.TckReport(
                profile="compiler",
                results=(graphblocks_testing.TckResult("compiler", "compiler", "passed"),),
            )
        },
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
    )

    assert not validation.ok
    assert validation.issue_contracts() == [
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "application-events",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.application-events",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "application-protocol",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.application-protocol",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "conversation",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.conversation",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "documents",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.documents",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "rag",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.rag",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "retry",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.retry",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "runtime",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.runtime",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "schema",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.schema",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "sequence",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.sequence",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "tool-execution",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.tool-execution",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "tool-lifecycle",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.tool-lifecycle",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
        {
            "code": "ConformanceTckMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "tool-result",
            "path": "$.profiles.GB-C2-AI-APPLICATION.tck.tool-result",
            "message": "claimed conformance profile requires a passing TCK suite with no report",
        },
    ]
