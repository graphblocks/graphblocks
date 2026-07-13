from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]


def _import_testing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-testing" / "src"))
    return importlib.import_module("graphblocks_testing")


def _tck_manifests(graphblocks_testing):
    return {
        manifest.suite_id: manifest
        for manifest in graphblocks_testing.load_tck_suite_manifests(ROOT / "tck")
    }


def _passing_tck_report(graphblocks_testing, suite, manifests):
    return graphblocks_testing.TckReport(
        profile="local",
        suite=suite,
        implementation="graphblocks-python",
        implementation_version="0.1.0",
        fixture_digest=manifests[suite].fixture_digest,
        results=tuple(
            graphblocks_testing.TckResult(case_id, suite, "passed")
            for case_id in manifests[suite].case_ids
        ),
    )


def _tck_implementations(manifests):
    return {suite: ("graphblocks-python", "0.1.0") for suite in manifests}


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_yaml_documents(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document is not None]


def _passing_acceptance_report(graphblocks_testing, manifest, application_ids):
    application_reports = []
    for application_id in application_ids:
        application = manifest.by_id(application_id)
        application_reports.append(
            graphblocks_testing.AcceptanceApplicationReport(
                application_id=application_id,
                scenario_path=application.scenario_path,
                application_digest=graphblocks_testing.canonical_hash(
                    application.application_contract()
                ),
                scenario_digest=graphblocks_testing.canonical_hash(
                    _load_yaml_documents(ROOT / application.scenario_path)
                ),
                results=tuple(
                    graphblocks_testing.AcceptanceGateResult(
                        application_id=application_id,
                        gate=gate,
                        status="passed",
                        output_digest=graphblocks_testing.canonical_hash(
                            {"gate": gate, "ok": True}
                        ),
                    )
                    for gate in application.gates
                ),
            )
        )
    return graphblocks_testing.AcceptanceRunReport(
        manifest_digest=manifest.content_digest(),
        applications=tuple(application_reports),
    )


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


def test_profiled_scenarios_are_declared_acceptance_applications(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )

    scenario_paths = {
        application.scenario_path for application in manifest.applications
    }

    assert scenario_paths >= {
        "acceptance/scenarios/direct-file-analysis.yaml",
        "acceptance/scenarios/multi-turn-chat.yaml",
        "examples/01-enterprise-federated-rag/example.yaml",
        "examples/02-document-ingestion/example.yaml",
        "examples/06-bounded-research-orchestrator/example.yaml",
        "examples/07-verified-rtl-workspace-trial/example.yaml",
        "examples/08-kubernetes-production-deployment/example.yaml",
        "examples/09-observability-profile/example.yaml",
        "examples/10-realtime-voice-extension/example.yaml",
        "examples/11-coding-agent-background-callbacks/example.yaml",
    }


def test_local_acceptance_scenarios_pass_declared_builtin_gates(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    runner = graphblocks_testing.AcceptanceGateRunner()

    for application in manifest.applications:
        builtin_gates = tuple(
            gate for gate in application.gates if gate.startswith("graphblocks ")
        )
        if not builtin_gates:
            continue
        builtin_application = graphblocks_testing.AcceptanceApplication(
            application_id=application.application_id,
            profiles=application.profiles,
            scenario_path=application.scenario_path,
            gates=builtin_gates,
            description=application.description,
        )

        report = runner.run_application(builtin_application, root=ROOT)

        assert report.ok, report.report_contract()


def test_acceptance_manifest_entries_are_stable_contracts(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    enterprise_rag = manifest.by_id("enterprise-rag")

    assert enterprise_rag.application_contract() == {
        "application_id": "enterprise-rag",
        "profiles": ["GB-C2-AI-APPLICATION"],
        "scenario_path": "examples/01-enterprise-federated-rag/example.yaml",
        "gates": [
            "graphblocks validate",
            "graphblocks plan --expand",
            "rag citation validation",
            "abstention check",
        ],
        "description": "Federated enterprise RAG with dense and keyword retrieval, fusion, rerank, budgeted context, abstention, and citation checks.",
    }
    assert manifest.content_digest().startswith("sha256:")


def test_acceptance_gate_runner_executes_exact_builtin_and_custom_handlers(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "acceptance-runner"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    manifest = graphblocks_testing.AcceptanceManifest(
        (
            graphblocks_testing.AcceptanceApplication(
                application_id="runner-smoke",
                profiles=("GB-C0-SCHEMA",),
                scenario_path="scenario.yaml",
                gates=(
                    "graphblocks validate",
                    "graphblocks plan --expand",
                    "semantic check",
                ),
            ),
        )
    )
    custom_calls = []

    def semantic_check(application, scenario_path):
        custom_calls.append((application.application_id, scenario_path))
        return 0, "semantic evidence passed"

    report = graphblocks_testing.AcceptanceGateRunner(
        custom_handlers={"semantic check": semantic_check}
    ).run_manifest(manifest, root=tmp_path)

    assert report.ok
    assert custom_calls == [("runner-smoke", scenario)]
    application_report = report.by_id("runner-smoke")
    assert [result.status for result in application_report.results] == [
        "passed",
        "passed",
        "passed",
    ]
    assert application_report.results[0].command == (
        "graphblocks",
        "validate",
        "scenario.yaml",
    )
    assert application_report.results[1].command == (
        "graphblocks",
        "plan",
        "scenario.yaml",
        "--expand",
    )
    assert report.content_digest().startswith("sha256:")


def test_acceptance_gate_runner_executes_authenticated_coding_agent_semantic_gates(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    application = manifest.by_id("coding-agent-background-callbacks")

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=ROOT,
    )
    repeated = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=ROOT,
    )

    assert report.ok, report.report_contract()
    assert [result.gate for result in report.results] == [
        "graphblocks validate",
        "accepted invocation handle check",
        "cursor replay after detach",
        "callback journal-before-resume check",
        "signed webhook delivery check",
    ]
    assert all(result.output_digest.startswith("sha256:") for result in report.results)
    assert repeated.report_contract() == report.report_contract()


def test_default_runner_executes_all_c2_semantic_gates(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    runner = graphblocks_testing.AcceptanceGateRunner()

    reports = tuple(
        runner.run_application(manifest.by_id(application_id), root=ROOT)
        for application_id in (
            "direct-file-analysis",
            "document-ingestion",
            "enterprise-rag",
            "multi-turn-chat",
        )
    )

    assert all(report.ok for report in reports), {
        report.application_id: [result.gate for result in report.results if not result.ok]
        for report in reports
        if not report.ok
    }


def test_document_ingestion_semantic_gates_build_canonical_knowledge_previews(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id("document-ingestion")
    application = graphblocks_testing.AcceptanceApplication(
        application_id=original.application_id,
        profiles=original.profiles,
        scenario_path=original.scenario_path,
        gates=("parser fallback check", "ACL propagation check"),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=ROOT,
    )

    assert report.ok, report.report_contract()
    assert [result.status for result in report.results] == ["passed", "passed"]


def test_default_runner_executes_orchestration_and_verified_trial_semantic_gates(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    runner = graphblocks_testing.AcceptanceGateRunner()

    reports = tuple(
        runner.run_application(manifest.by_id(application_id), root=ROOT)
        for application_id in (
            "bounded-research-orchestrator",
            "verified-rtl-workspace-trial",
        )
    )
    repeated = tuple(
        runner.run_application(manifest.by_id(application_id), root=ROOT)
        for application_id in (
            "bounded-research-orchestrator",
            "verified-rtl-workspace-trial",
        )
    )

    assert all(report.ok for report in reports), {
        report.application_id: [result.gate for result in report.results if not result.ok]
        for report in reports
        if not report.ok
    }
    assert [result.gate for result in reports[0].results] == [
        "graphblocks validate",
        "graphblocks plan --expand",
        "bounded task plan check",
        "task budget delegation check",
        "replan patch CAS check",
    ]
    assert [result.gate for result in reports[1].results] == [
        "graphblocks validate",
        "budget lease reservation check",
        "review invalidation check",
        "governed trial commit gate",
    ]
    assert [report.report_contract() for report in repeated] == [
        report.report_contract() for report in reports
    ]


@pytest.mark.parametrize(
    ("application_id", "gate"),
    (
        ("bounded-research-orchestrator", "bounded task plan check"),
        ("bounded-research-orchestrator", "task budget delegation check"),
        ("bounded-research-orchestrator", "replan patch CAS check"),
        ("verified-rtl-workspace-trial", "budget lease reservation check"),
        ("verified-rtl-workspace-trial", "review invalidation check"),
        ("verified-rtl-workspace-trial", "governed trial commit gate"),
    ),
)
def test_orchestration_and_trial_semantic_gates_reject_weakened_scenarios(
    monkeypatch,
    tmp_path,
    application_id,
    gate,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id(application_id)
    documents = _load_yaml_documents(ROOT / original.scenario_path)
    graph = documents[0]
    nodes = graph["spec"]["nodes"]
    if gate == "bounded task plan check":
        nodes["plan"]["config"]["limits"]["maxDepth"] = 0
    elif gate == "task budget delegation check":
        nodes["execute"]["config"]["reservation"] = "shared"
    elif gate == "replan patch CAS check":
        nodes["patch"]["config"]["concurrency"] = "last_write_wins"
    elif gate == "budget lease reservation check":
        documents[1]["spec"]["nodes"]["formal"]["flow"] = {}
    elif gate == "review invalidation check":
        nodes["review"]["config"]["invalidateOnSubjectChange"] = False
    else:
        nodes["verifyTrial"]["config"]["requiredChecks"] = ["lint", "compile", "regression"]
    scenario = tmp_path / f"{application_id}.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id=application_id,
        profiles=original.profiles,
        scenario_path=scenario.name,
        gates=(gate,),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=tmp_path,
    )

    assert not report.ok, report.report_contract()


def test_default_acceptance_gate_runner_executes_full_manifest(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_manifest(
        manifest,
        root=ROOT,
    )

    assert report.ok, {
        application.application_id: [
            result.gate for result in application.results if not result.ok
        ]
        for application in report.applications
        if not application.ok
    }


def test_default_runner_executes_deployment_voice_and_telemetry_semantic_gates(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    runner = graphblocks_testing.AcceptanceGateRunner()

    reports = tuple(
        runner.run_application(manifest.by_id(application_id), root=ROOT)
        for application_id in (
            "kubernetes-canary",
            "realtime-voice-agent",
            "telemetry-outage-correctness",
        )
    )
    repeated = tuple(
        runner.run_application(manifest.by_id(application_id), root=ROOT)
        for application_id in (
            "kubernetes-canary",
            "realtime-voice-agent",
            "telemetry-outage-correctness",
        )
    )

    assert all(report.ok for report in reports), {
        report.application_id: [result.gate for result in report.results if not result.ok]
        for report in reports
        if not report.ok
    }
    assert [report.report_contract() for report in repeated] == [
        report.report_contract() for report in reports
    ]


@pytest.mark.parametrize(
    ("application_id", "gate"),
    (
        ("kubernetes-canary", "release bundle verification"),
        ("kubernetes-canary", "canary quality gate"),
        ("kubernetes-canary", "rollback and drain gate"),
        ("realtime-voice-agent", "duplex session contract check"),
        ("realtime-voice-agent", "interruption authority check"),
        ("realtime-voice-agent", "playback ledger check"),
        ("telemetry-outage-correctness", "OTel projection check"),
        ("telemetry-outage-correctness", "Langfuse projection check"),
        ("telemetry-outage-correctness", "telemetry outage correctness check"),
    ),
)
def test_production_extension_semantic_gates_reject_weakened_scenarios(
    monkeypatch,
    tmp_path,
    application_id,
    gate,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id(application_id)
    documents = _load_yaml_documents(ROOT / original.scenario_path)
    if application_id == "kubernetes-canary":
        release, deployment = documents
        if gate == "release bundle verification":
            release["spec"]["bundle"]["signaturePolicy"] = "none"
        elif gate == "canary quality gate":
            deployment["spec"]["rollout"]["gates"][2].pop("maxRegression")
        else:
            deployment["spec"]["upgrades"]["existingRequests"] = "migrate"
    elif application_id == "realtime-voice-agent":
        graph = documents[0]
        if gate == "duplex session contract check":
            graph["spec"]["execution"]["interaction"] = "incremental"
        elif gate == "interruption authority check":
            graph["spec"]["voice"]["localVad"]["role"] = "authoritative"
        else:
            graph["spec"]["voice"]["playback"]["acknowledgements"] = "optional"
    else:
        profile = documents[0]
        if gate == "OTel projection check":
            profile["spec"]["exporters"]["otlp"]["endpoint"] = ""
        elif gate == "Langfuse projection check":
            profile["spec"]["exporters"]["langfuse"]["mode"] = "direct"
        else:
            profile["spec"]["durableRecords"]["auditLog"]["delivery"] = "best_effort"
    scenario = tmp_path / f"{application_id}.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id=application_id,
        profiles=original.profiles,
        scenario_path=scenario.name,
        gates=(gate,),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(application, root=tmp_path)

    assert not report.ok, report.report_contract()


@pytest.mark.parametrize("weakening", ("release_ref", "mutable_identity"))
def test_release_verification_binds_deployment_reference_and_release_identity(
    monkeypatch,
    tmp_path,
    weakening,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id("kubernetes-canary")
    documents = _load_yaml_documents(ROOT / original.scenario_path)
    if weakening == "release_ref":
        documents[1]["spec"]["releaseRef"]["name"] = "unverified-release"
    else:
        documents[0]["spec"]["identity"]["graphHash"] = "latest"
    scenario = tmp_path / f"kubernetes-canary-{weakening}.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id="kubernetes-canary",
        profiles=original.profiles,
        scenario_path=scenario.name,
        gates=("release bundle verification",),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(application, root=tmp_path)

    assert not report.ok, report.report_contract()


def test_telemetry_gates_reject_allowed_and_forbidden_dimension_overlap(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id("telemetry-outage-correctness")
    documents = _load_yaml_documents(ROOT / original.scenario_path)
    documents[0]["spec"]["metrics"]["dimensions"].append("run_id")
    scenario = tmp_path / "telemetry-cardinality-overlap.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id="telemetry-outage-correctness",
        profiles=original.profiles,
        scenario_path=scenario.name,
        gates=("OTel projection check",),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(application, root=tmp_path)

    assert not report.ok, report.report_contract()


@pytest.mark.parametrize(
    ("application_id", "gate"),
    (
        ("direct-file-analysis", "generated artifact check"),
        ("document-ingestion", "ACL propagation check"),
        ("enterprise-rag", "rag citation validation"),
        ("multi-turn-chat", "conversation CAS check"),
    ),
)
def test_c2_semantic_gates_reject_weakened_scenario_dataflow(
    monkeypatch,
    tmp_path,
    application_id,
    gate,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    original = manifest.by_id(application_id)
    documents = _load_yaml_documents(ROOT / original.scenario_path)
    graph = next(document for document in documents if document.get("kind") == "Graph")
    if application_id == "direct-file-analysis":
        graph["spec"]["nodes"]["generateArtifact"]["block"] = "control.identity@1"
    elif application_id == "document-ingestion":
        item_graph = next(
            document
            for document in documents
            if document.get("metadata", {}).get("name") == "process-single-asset"
        )
        item_graph["spec"]["nodes"]["persist"]["config"] = {"requireAclRevision": False}
    elif application_id == "enterprise-rag":
        graph["spec"]["nodes"]["validate"]["block"] = "control.identity@1"
    else:
        graph["spec"]["nodes"]["commitTurn"]["inputs"]["response"] = "beginTurn.message"
    scenario = tmp_path / f"{application_id}.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id=application_id,
        profiles=original.profiles,
        scenario_path=scenario.name,
        gates=(gate,),
        description=original.description,
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=tmp_path,
    )

    assert not report.ok, report.report_contract()


def test_callback_acceptance_gate_rejects_scenario_with_weakened_resume_fence(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    documents = _load_yaml_documents(
        ROOT / "acceptance" / "scenarios" / "coding-agent-background-callbacks.yaml"
    )
    documents[1]["spec"]["nodes"]["waitCI"]["config"]["resume"][
        "requirePolicyReevaluation"
    ] = False
    scenario = tmp_path / "weakened-coding-agent.yaml"
    scenario.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    application = graphblocks_testing.AcceptanceApplication(
        application_id="coding-agent-background-callbacks",
        profiles=("GB-C4-PRODUCTION",),
        scenario_path=scenario.name,
        gates=("callback journal-before-resume check",),
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        application,
        root=tmp_path,
    )

    assert not report.ok
    assert report.results[0].diagnostic_contracts() == [
        {
            "code": "AcceptanceGateExecutionFailed",
            "message": "coding-agent callback resume fences do not match the production contract",
            "path": "$.applications.coding-agent-background-callbacks.gates[0]",
        }
    ]


def test_coding_agent_semantic_gates_cannot_be_replaced_by_custom_handlers(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    application = manifest.by_id("coding-agent-background-callbacks")
    signed_application = graphblocks_testing.AcceptanceApplication(
        application_id=application.application_id,
        profiles=application.profiles,
        scenario_path=application.scenario_path,
        gates=("signed webhook delivery check",),
        description=application.description,
    )
    custom_calls: list[str] = []

    def always_pass(application, scenario_path):
        custom_calls.append(application.application_id)
        return 0, "fabricated"

    report = graphblocks_testing.AcceptanceGateRunner(
        custom_handlers={"signed webhook delivery check": always_pass}
    ).run_application(signed_application, root=ROOT)

    assert report.ok, report.report_contract()
    assert custom_calls == []


def test_signed_webhook_acceptance_gate_fails_closed_without_callback_support(
    monkeypatch,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    application = manifest.by_id("coding-agent-background-callbacks")
    signed_application = graphblocks_testing.AcceptanceApplication(
        application_id=application.application_id,
        profiles=application.profiles,
        scenario_path=application.scenario_path,
        gates=("signed webhook delivery check",),
        description=application.description,
    )
    real_import_module = graphblocks_testing.importlib.import_module

    def missing_callbacks(name, package=None):
        if name == "graphblocks.callbacks":
            raise ModuleNotFoundError(name)
        return real_import_module(name, package)

    monkeypatch.setattr(graphblocks_testing.importlib, "import_module", missing_callbacks)

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        signed_application,
        root=ROOT,
    )

    assert not report.ok
    assert report.results[0].diagnostic_contracts() == [
        {
            "code": "AcceptanceGateExecutionFailed",
            "message": "signed webhook delivery check requires GraphBlocks callback support",
            "path": "$.applications.coding-agent-background-callbacks.gates[0]",
        }
    ]


@pytest.mark.parametrize(
    ("application_id", "gate", "missing_module", "expected_message"),
    [
        (
            "realtime-voice-agent",
            "duplex session contract check",
            "graphblocks.voice",
            "voice semantic gates require bundled GraphBlocks voice support",
        ),
        (
            "telemetry-outage-correctness",
            "OTel projection check",
            "graphblocks.audit",
            "telemetry semantic gates require bundled GraphBlocks observability support",
        ),
    ],
)
def test_acceptance_gates_describe_missing_bundled_support(
    monkeypatch,
    application_id: str,
    gate: str,
    missing_module: str,
    expected_message: str,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    application = manifest.by_id(application_id)
    focused_application = graphblocks_testing.AcceptanceApplication(
        application_id=application.application_id,
        profiles=application.profiles,
        scenario_path=application.scenario_path,
        gates=(gate,),
        description=application.description,
    )
    real_import_module = graphblocks_testing.importlib.import_module

    def missing_support(name, package=None):
        if name == missing_module:
            raise ModuleNotFoundError(name)
        return real_import_module(name, package)

    monkeypatch.setattr(graphblocks_testing.importlib, "import_module", missing_support)

    report = graphblocks_testing.AcceptanceGateRunner().run_application(
        focused_application,
        root=ROOT,
    )

    assert not report.ok
    assert report.results[0].diagnostic_contracts()[0]["message"] == expected_message


def test_acceptance_gate_runner_never_evaluates_unknown_shell_text(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "acceptance-no-shell"},
                "spec": {"nodes": {}},
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "must-not-exist"
    manifest = graphblocks_testing.AcceptanceManifest(
        (
            graphblocks_testing.AcceptanceApplication(
                application_id="no-shell",
                profiles=("GB-C0-SCHEMA",),
                scenario_path="scenario.yaml",
                gates=(f"graphblocks validate; touch {marker}",),
            ),
        )
    )

    report = graphblocks_testing.AcceptanceGateRunner().run_manifest(
        manifest,
        root=tmp_path,
    )

    assert not report.ok
    assert not marker.exists()
    assert report.by_id("no-shell").results[0].diagnostic_contracts() == [
        {
            "code": "AcceptanceGateHandlerMissing",
            "message": "acceptance gate has no registered exact handler",
            "path": "$.applications.no-shell.gates[0]",
        }
    ]


def test_acceptance_failure_report_digest_is_stable_across_roots(
    monkeypatch,
    tmp_path,
) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest(
        (
            graphblocks_testing.AcceptanceApplication(
                application_id="missing-scenario",
                profiles=("GB-C0-SCHEMA",),
                scenario_path="missing.yaml",
                gates=("graphblocks validate", "root failure"),
            ),
        )
    )
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    def root_failure(application, scenario_path):
        return 1, f"failure under {scenario_path.parent}"

    runner = graphblocks_testing.AcceptanceGateRunner(
        custom_handlers={"root failure": root_failure}
    )
    first = runner.run_manifest(
        manifest,
        root=first_root,
    )
    second = runner.run_manifest(
        manifest,
        root=second_root,
    )

    assert not first.ok
    assert first.report_contract() == second.report_contract()
    assert first.content_digest() == second.content_digest()


def test_acceptance_coverage_without_root_cannot_satisfy_evidence(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    manifest = graphblocks_testing.AcceptanceManifest(
        (
            graphblocks_testing.AcceptanceApplication(
                application_id="unrooted",
                profiles=("GB-C0-SCHEMA",),
                scenario_path="scenario.yaml",
                gates=("graphblocks validate",),
            ),
        )
    )

    coverage = manifest.coverage_for_conformance(
        {
            "spec": {
                "profiles": [
                    {
                        "id": "GB-C0-SCHEMA",
                        "acceptanceApplications": ["unrooted"],
                    }
                ]
            }
        }
    )

    assert not coverage.ok
    assert coverage.issue_contracts() == [
        {
            "code": "AcceptanceScenarioDigestMissing",
            "application_id": "unrooted",
            "profile_id": "",
            "path": "$.spec.applications[unrooted].scenarioPath",
            "message": "acceptance evidence requires a root to digest the scenario",
        }
    ]


def test_acceptance_reports_reject_noncanonical_digests_and_empty_runs(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)

    with pytest.raises(ValueError, match="acceptance gate output_digest must be a canonical sha256 digest"):
        graphblocks_testing.AcceptanceGateResult(
            application_id="invalid-digest",
            gate="semantic check",
            status="passed",
            output_digest="not-a-digest",
        )

    empty = graphblocks_testing.AcceptanceRunReport(
        manifest_digest="sha256:" + ("0" * 64),
        applications=(),
    )
    assert not empty.ok


def test_coding_agent_background_callback_example_matches_async_contract() -> None:
    application, graph = _load_yaml_documents(
        ROOT / "examples" / "11-coding-agent-background-callbacks" / "example.yaml"
    )
    callback = application["spec"]["callbackRegistration"]

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
    assert routes["reply-tool-permission"] == {
        "id": "reply-tool-permission",
        "method": "POST",
        "path": "/v1/coding/tasks/{run_id}/permissions/{approval_id}",
        "command": "SubmitApproval",
    }

    assert graph["kind"] == "Graph"
    assert graph["metadata"]["name"] == "coding-agent-task"
    assert graph["spec"]["execution"] == {
        "lifetime": "job",
        "durability": "checkpointed",
        "interaction": "incremental",
    }
    assert graph["spec"]["eventStream"]["replayable"] is True
    nodes = graph["spec"]["nodes"]
    assert nodes["discoverInstructions"] == {
        "block": "examples.opencode.discover_instructions@1",
        "inputs": {
            "workspace": "snapshot.value",
            "workingDirectory": "begin.working_directory",
        },
        "config": {
            "projectFiles": ["AGENTS.md", "CLAUDE.md"],
            "customInstructionConfig": "opencode.json",
            "traversal": "working_directory_to_worktree",
            "nestedInstructions": "lazy_on_file_read",
        },
    }
    session_config = nodes["agentSession"]["config"]
    assert nodes["agentSession"]["block"] == "examples.opencode.agent_session@1"
    assert session_config["loop"] == "model_turn_tools_until_final"
    assert session_config["snapshotBeforeStep"] is True
    assert session_config["patchAfterStep"] is True
    assert session_config["permissions"] == {
        "default": "allow",
        "external_directory": "ask",
        "doom_loop": "ask",
        "read": {
            "*": "allow",
            "*.env": "deny",
            "*.env.*": "deny",
            "*.env.example": "allow",
        },
        "responses": ["once", "always", "reject"],
    }
    assert session_config["toolParts"] == {
        "states": ["pending", "running", "completed", "error"],
        "checkpoint": "each_result",
        "onFailure": "return_to_model",
    }
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
    assert nodes["startCI"]["config"]["idempotencyKey"] == "provider_delivery_id"
    assert nodes["startCI"]["config"]["attemptFencing"] is True
    assert nodes["startCI"]["config"]["resume"] == {
        "requirePolicyReevaluation": True,
        "requireBudgetReservation": True,
        "requireReleaseCompatibility": True,
        "requireOwnershipFence": True,
    }
    assert nodes["waitCI"] == {
        "block": "async.await_callback@1",
        "inputs": {"operation": "startCI.operation"},
        "config": {
            "checkpoint": True,
            "timeout": "30m",
            "idempotencyKey": "provider_delivery_id",
            "attemptFencing": True,
            "callback": {"schema": "schemas/CICallback@1"},
            "resume": {
                "requirePolicyReevaluation": True,
                "requireBudgetReservation": True,
                "requireReleaseCompatibility": True,
                "requireOwnershipFence": True,
            },
            "onTimeout": "fail",
        },
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


def test_local_coding_agent_scenario_binds_callback_registration_evidence() -> None:
    example_application, _ = _load_yaml_documents(
        ROOT / "examples" / "11-coding-agent-background-callbacks" / "example.yaml"
    )
    local_application, _ = _load_yaml_documents(
        ROOT / "acceptance" / "scenarios" / "coding-agent-background-callbacks.yaml"
    )

    assert local_application["spec"]["callbackRegistration"] == example_application["spec"][
        "callbackRegistration"
    ]


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
    acceptance_report = _passing_acceptance_report(
        graphblocks_testing,
        manifest,
        (
            "direct-file-analysis",
            "document-ingestion",
            "enterprise-rag",
            "multi-turn-chat",
        ),
    )
    manifests = _tck_manifests(graphblocks_testing)
    passing_reports = {
        suite: _passing_tck_report(graphblocks_testing, suite, manifests)
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
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=acceptance_coverage,
        acceptance_report=acceptance_report,
    )

    assert validation.ok
    assert validation.issue_contracts() == []
    assert validation.claim.profile_ids == ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME", "GB-C2-AI-APPLICATION")


def test_conformance_profile_claim_rejects_coverage_without_execution_report(monkeypatch) -> None:
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
    manifests = _tck_manifests(graphblocks_testing)
    passing_reports = {
        suite: _passing_tck_report(graphblocks_testing, suite, manifests)
        for suite in profile_set.claim_requirements(
            ("GB-C2-AI-APPLICATION",)
        ).tck_suites
    }

    validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports=passing_reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=acceptance_coverage,
    )

    assert not validation.ok
    assert validation.issue_contracts() == [
        {
            "code": "ConformanceAcceptanceReportMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "acceptance",
            "path": "$.profiles.GB-C2-AI-APPLICATION.acceptance",
            "message": "claimed conformance profile requires executed acceptance reports",
        }
    ]


def test_conformance_profile_claim_rejects_unidentified_tck_evidence(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    claim = profile_set.claim_requirements(("GB-C0-SCHEMA",))
    manifests = _tck_manifests(graphblocks_testing)
    reports = {
        suite: graphblocks_testing.TckReport(
            profile="local",
            suite=suite,
            implementation="",
            implementation_version="",
            fixture_digest="sha256:" + ("0" * 64),
            results=(graphblocks_testing.TckResult(suite, suite, "passed"),),
        )
        for suite in claim.tck_suites
    }

    validation = profile_set.validate_claim(
        ("GB-C0-SCHEMA",),
        tck_reports=reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
    )

    assert not validation.ok
    assert {issue.code for issue in validation.issues} == {"ConformanceTckEvidenceInvalid"}


def test_conformance_profile_claim_rejects_stale_tck_fixture_digest(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    claim = profile_set.claim_requirements(("GB-C0-SCHEMA",))
    manifests = _tck_manifests(graphblocks_testing)
    reports = {
        suite: graphblocks_testing.TckReport(
            profile="local",
            suite=suite,
            implementation="graphblocks-python",
            implementation_version="0.1.0",
            fixture_digest="sha256:" + ("0" * 64),
            results=(graphblocks_testing.TckResult(suite, suite, "passed"),),
        )
        for suite in claim.tck_suites
    }

    validation = profile_set.validate_claim(
        ("GB-C0-SCHEMA",),
        tck_reports=reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
    )

    assert not validation.ok
    assert {issue.code for issue in validation.issues} == {"ConformanceTckEvidenceStale"}


def test_conformance_profile_claim_binds_case_coverage_and_implementation(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    claim = profile_set.claim_requirements(("GB-C0-SCHEMA",))
    manifests = _tck_manifests(graphblocks_testing)
    expectations = {
        suite: ("graphblocks-python", "0.1.0") for suite in claim.tck_suites
    }
    complete = {
        suite: _passing_tck_report(graphblocks_testing, suite, manifests)
        for suite in claim.tck_suites
    }
    incomplete = dict(complete)
    incomplete["schema"] = graphblocks_testing.TckReport(
        profile="local",
        suite="schema",
        implementation="graphblocks-python",
        implementation_version="0.1.0",
        fixture_digest=manifests["schema"].fixture_digest,
        results=complete["schema"].results[:-1],
    )
    wrong_implementation = dict(complete)
    wrong_implementation["schema"] = graphblocks_testing.TckReport(
        profile="local",
        suite="schema",
        implementation="unknown-runner",
        implementation_version="9.9.9",
        fixture_digest=manifests["schema"].fixture_digest,
        results=complete["schema"].results,
    )

    incomplete_validation = profile_set.validate_claim(
        ("GB-C0-SCHEMA",),
        tck_reports=incomplete,
        tck_manifests=manifests,
        tck_implementations=expectations,
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
    )
    implementation_validation = profile_set.validate_claim(
        ("GB-C0-SCHEMA",),
        tck_reports=wrong_implementation,
        tck_manifests=manifests,
        tck_implementations=expectations,
        acceptance_coverage=graphblocks_testing.AcceptanceCoverageResult(),
    )

    assert {issue.code for issue in incomplete_validation.issues} == {
        "ConformanceTckCoverageInvalid"
    }
    assert {issue.code for issue in implementation_validation.issues} == {
        "ConformanceTckImplementationMismatch"
    }


def test_conformance_profile_claim_rejects_failed_and_stale_acceptance_reports(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    manifest = graphblocks_testing.AcceptanceManifest.from_document(
        _load_yaml(ROOT / "acceptance" / "applications.yaml")
    )
    coverage = manifest.coverage_for_conformance(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"),
        root=ROOT,
    )
    claim = profile_set.claim_requirements(("GB-C2-AI-APPLICATION",))
    manifests = _tck_manifests(graphblocks_testing)
    tck_reports = {
        suite: _passing_tck_report(graphblocks_testing, suite, manifests)
        for suite in claim.tck_suites
    }
    passing = _passing_acceptance_report(
        graphblocks_testing,
        manifest,
        claim.acceptance_applications,
    )
    failed_application = passing.by_id("enterprise-rag")
    failed_report = graphblocks_testing.AcceptanceRunReport(
        manifest_digest=passing.manifest_digest,
        applications=tuple(
            graphblocks_testing.AcceptanceApplicationReport(
                application_id=application.application_id,
                scenario_path=application.scenario_path,
                application_digest=application.application_digest,
                scenario_digest=application.scenario_digest,
                results=tuple(
                    graphblocks_testing.AcceptanceGateResult(
                        application_id=application.application_id,
                        gate=result.gate,
                        status="failed" if result_index == 0 else result.status,
                        command=result.command,
                        output_digest=graphblocks_testing.canonical_hash(
                            {"gate": result.gate, "ok": result_index != 0}
                        ),
                        diagnostics=result.diagnostics,
                    )
                    for result_index, result in enumerate(application.results)
                ),
            )
            if application.application_id == failed_application.application_id
            else application
            for application in passing.applications
        ),
    )
    stale_report = graphblocks_testing.AcceptanceRunReport(
        manifest_digest="sha256:" + ("0" * 64),
        applications=passing.applications,
    )
    forged_application = passing.by_id("enterprise-rag")
    forged_report = graphblocks_testing.AcceptanceRunReport(
        manifest_digest=passing.manifest_digest,
        applications=tuple(
            graphblocks_testing.AcceptanceApplicationReport(
                application_id=application.application_id,
                scenario_path="wrong.yaml",
                application_digest="sha256:" + ("9" * 64),
                scenario_digest="sha256:" + ("8" * 64),
                results=(
                    graphblocks_testing.AcceptanceGateResult(
                        application_id=application.application_id,
                        gate="not declared",
                        status="passed",
                        output_digest=graphblocks_testing.canonical_hash({"forged": True}),
                    ),
                ),
            )
            if application.application_id == forged_application.application_id
            else application
            for application in passing.applications
        ),
    )

    failed_validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports=tck_reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=coverage,
        acceptance_report=failed_report,
    )
    stale_validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports=tck_reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=coverage,
        acceptance_report=stale_report,
    )
    forged_validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports=tck_reports,
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
        acceptance_coverage=coverage,
        acceptance_report=forged_report,
    )

    assert [issue["code"] for issue in failed_validation.issue_contracts()] == [
        "ConformanceAcceptanceReportFailed"
    ]
    assert [issue["code"] for issue in stale_validation.issue_contracts()] == [
        "ConformanceAcceptanceReportStale"
    ]
    assert [issue["code"] for issue in forged_validation.issue_contracts()] == [
        "ConformanceAcceptanceReportStale"
    ]


def test_conformance_profile_claim_reports_missing_inherited_tck(monkeypatch) -> None:
    graphblocks_testing = _import_testing(monkeypatch)
    profile_set = graphblocks_testing.ConformanceProfileSet.from_document(
        _load_yaml(ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml")
    )
    manifests = _tck_manifests(graphblocks_testing)

    validation = profile_set.validate_claim(
        ("GB-C2-AI-APPLICATION",),
        tck_reports={
            "compiler": _passing_tck_report(
                graphblocks_testing,
                "compiler",
                manifests,
            )
        },
        tck_manifests=manifests,
        tck_implementations=_tck_implementations(manifests),
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
        {
            "code": "ConformanceAcceptanceReportMissing",
            "profile_id": "GB-C2-AI-APPLICATION",
            "suite": "acceptance",
            "path": "$.profiles.GB-C2-AI-APPLICATION.acceptance",
            "message": "claimed conformance profile requires executed acceptance reports",
        },
    ]
