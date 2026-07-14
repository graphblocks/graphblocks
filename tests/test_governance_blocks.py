from __future__ import annotations

import pytest

from graphblocks.runtime import InProcessRuntime, stdlib_registry
from graphblocks.stdlib_governance import (
    GOVERNANCE_BLOCKS,
    check_run_suite_block,
    gate_evaluate_block,
    result_bundle_block,
    review_request_block,
    structured_generate_block,
)


CONTEXT = {"run_id": "run-42", "node": "test-node"}
SUBJECT = {"resourceId": "snapshot-1", "digest": "sha256:subject", "resourceKind": "workspace"}


def test_governance_block_catalog_contains_every_executable_identity() -> None:
    assert set(GOVERNANCE_BLOCKS) == {
        "model.structured_generate@1",
        "check.run_suite@1",
        "gate.evaluate@1",
        "review.request@1",
        "result.bundle@1",
    }
    assert all(callable(block) for block in GOVERNANCE_BLOCKS.values())


def test_stock_registry_resolves_every_documented_domain_block() -> None:
    expected = {
        "model.structured_generate@1",
        "retrieve.execute_plan@1",
        "retrieve.fuse@1",
        "rank.documents@1",
        "context.build@1",
        "answer.validate_grounding@1",
        "check.run_suite@1",
        "gate.evaluate@1",
        "review.request@1",
        "result.bundle@1",
    }

    assert expected <= set(stdlib_registry().blocks)


def test_governance_blocks_execute_as_one_stock_runtime_graph() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "executable-governance-blocks"},
        "spec": {
            "nodes": {
                "generate": {
                    "block": "model.structured_generate@1",
                    "inputs": {"response": "$input.response"},
                    "config": {"outputSchema": "company/Patch@1"},
                },
                "checks": {
                    "block": "check.run_suite@1",
                    "inputs": {"subject": "generate.value"},
                    "config": {
                        "checks": ["schema"],
                        "outcomes": {"schema": "passed"},
                    },
                },
                "gate": {
                    "block": "gate.evaluate@1",
                    "inputs": {"checks": "checks.results"},
                    "config": {
                        "gateId": "publish",
                        "hardConstraints": ["schema"],
                    },
                },
                "review": {
                    "block": "review.request@1",
                    "inputs": {
                        "subject": "generate.value",
                        "gate": "gate.result",
                    },
                    "config": {
                        "scope": "correctness",
                        "createdAt": "2026-07-11T00:00:00Z",
                    },
                    "outputs": {"request": "$output.reviewRequest"},
                },
                "bundle": {
                    "block": "result.bundle@1",
                    "inputs": {
                        "outputs": "$input.outputs",
                        "checks": "checks.results",
                        "gate": "gate.result",
                    },
                    "config": {
                        "releaseId": "release-1",
                        "graphHash": "sha256:graph",
                        "startedAt": "2026-07-11T00:00:00Z",
                    },
                    "outputs": {"result": "$output.result"},
                },
            }
        },
    }

    execution = InProcessRuntime(stdlib_registry()).run(
        graph,
        {
            "response": '{"patch":"diff"}',
            "outputs": [{"schemaId": "company/Patch@1", "patch": "diff"}],
        },
        run_id="run-governance-blocks-1",
    )

    assert execution.status == "succeeded"
    assert execution.outputs["result"]["run_id"] == "run-governance-blocks-1"
    assert execution.outputs["result"]["checks"][0]["status"] == "passed"
    assert execution.outputs["reviewRequest"]["required_scopes"] == [
        "correctness"
    ]


def test_structured_generate_uses_provider_or_fixture_json_and_projects_items() -> None:
    result = structured_generate_block(
        {"response": '{"items":[{"candidate_id":"candidate-1"}]}'},
        {"outputSchema": "company.hdl/PatchCandidateSet@1"},
        CONTEXT,
    )

    assert result["schemaId"] == "company.hdl/PatchCandidateSet@1"
    assert result["items"] == [{"candidate_id": "candidate-1"}]
    assert result["value"] == {"items": result["items"]}
    assert str(result["contentDigest"]).startswith("sha256:")

    mapping_result = structured_generate_block(
        {"response": '{"answer":"done"}'},
        {"outputSchema": "company/Output@1"},
        CONTEXT,
    )
    assert mapping_result["items"] == []

    with pytest.raises(ValueError, match="requires inputs.response or config.response"):
        structured_generate_block({}, {"outputSchema": "company/Output@1"}, CONTEXT)


def test_check_suite_executes_configured_outcomes_and_fails_closed_when_unbound() -> None:
    result = check_run_suite_block(
        {"subject": SUBJECT},
        {
            "checks": ["lint", "compile"],
            "outcomes": {"lint": "passed"},
        },
        CONTEXT,
    )

    assert [item["status"] for item in result["results"]] == ["passed", "inconclusive"]
    assert result["passed"] is False
    assert result["diagnostics"] == [
        {
            "code": "CHECK_IMPLEMENTATION_UNBOUND",
            "message": "check 'compile' has no configured outcome or bound implementation",
            "path": "$",
            "severity": "warning",
        }
    ]


def test_gate_evaluate_reuses_typed_gate_semantics() -> None:
    result = gate_evaluate_block(
        {
            "checks": [
                {"checkId": "lint", "subject": SUBJECT, "status": "passed"},
                {"checkId": "compile", "subject": SUBJECT, "status": "failed"},
            ],
            "metrics": [{"name": "coverage", "value": 89}],
        },
        {
            "gateId": "release-gate",
            "hardConstraints": ["lint", "compile"],
            "constraints": [{"metric": "coverage", "operator": "at_least", "threshold": 90}],
        },
        CONTEXT,
    )

    assert result["decision"] == "fail"
    assert result["passed"] is False
    assert result["violations"] == ["check:compile", "metric:coverage"]
    assert result["result"]["gate_id"] == "release-gate"


def test_review_request_is_deterministic_and_does_not_fabricate_approval() -> None:
    config = {
        "scope": "design_intent",
        "createdAt": "2026-07-11T00:00:00Z",
        "requestedBy": {"principalId": "agent-1", "tenantId": "tenant-1"},
        "invalidateOnSubjectChange": True,
    }
    first = review_request_block({"subject": SUBJECT}, config, CONTEXT)
    second = review_request_block({"subject": SUBJECT}, config, CONTEXT)

    assert first == second
    assert first["status"] == "pending"
    assert first["pending"] is True
    assert first["accepted"] is False
    assert first["record"] is None
    assert first["waitMode"] == "application_event"
    assert first["request"]["required_scopes"] == ["design_intent"]


def test_review_request_accepts_only_subject_bound_credentialed_review() -> None:
    result = review_request_block(
        {
            "subject": SUBJECT,
            "review": {
                "reviewId": "review-1",
                "subjectDigest": "sha256:subject",
                "scope": "design_intent",
                "reviewer": {"principalId": "reviewer-1"},
                "decision": "accept",
                "credentialRefs": ["credential-1"],
                "createdAt": "2026-07-11T00:01:00Z",
            },
        },
        {
            "scope": "design_intent",
            "requiredCredential": "credential-1",
            "createdAt": "2026-07-11T00:00:00Z",
        },
        CONTEXT,
    )

    assert result["accepted"] is True
    assert result["pending"] is False
    assert result["record"]["decision"] == "accept"

    with pytest.raises(ValueError, match="subject digest must match"):
        review_request_block(
            {
                "subject": SUBJECT,
                "review": {
                    "reviewId": "review-2",
                    "subjectDigest": "sha256:stale",
                    "scope": "design_intent",
                    "reviewer": {"principalId": "reviewer-1"},
                    "decision": "accept",
                    "credentialRefs": ["credential-1"],
                    "createdAt": "2026-07-11T00:01:00Z",
                },
            },
            {
                "scope": "design_intent",
                "requiredCredential": "credential-1",
                "createdAt": "2026-07-11T00:00:00Z",
            },
            CONTEXT,
        )


def test_result_bundle_adapts_json_outputs_and_preserves_gate_checks() -> None:
    checks = check_run_suite_block(
        {"subject": SUBJECT},
        {"checks": ["lint"], "outcomes": {"lint": "passed"}},
        CONTEXT,
    )["results"]
    result = result_bundle_block(
        {
            "inputs": [SUBJECT],
            "outputs": [{"schemaId": "company/Patch@1", "patch": "diff"}],
            "checks": checks,
            "artifacts": [{"artifactId": "artifact-1", "uri": "memory://artifact-1"}],
            "evidence": [{"evidenceId": "evidence-1", "source": SUBJECT, "kind": "snapshot"}],
            "reviews": [
                {
                    "reviewId": "review-1",
                    "subject": SUBJECT,
                    "scope": "design_intent",
                    "reviewer": {"principalId": "reviewer-1"},
                    "decision": "accept",
                    "credentialRefs": ["credential-1"],
                    "createdAt": "2026-07-11T00:00:30Z",
                }
            ],
            "usage": ["usage-1"],
        },
        {
            "releaseId": "release-7",
            "graphHash": "sha256:graph",
            "startedAt": "2026-07-11T00:00:00Z",
            "completedAt": "2026-07-11T00:01:00Z",
        },
        CONTEXT,
    )

    bundle = result["result"]
    assert bundle == result["bundle"]
    assert bundle["run_id"] == "run-42"
    assert bundle["outputs"][0]["schema_id"] == "company/Patch@1"
    assert bundle["checks"][0]["status"] == "passed"
    assert bundle["artifacts"][0]["artifact_id"] == "artifact-1"
    assert bundle["evidence"][0]["evidence_id"] == "evidence-1"
    assert bundle["reviews"][0]["decision"] == "accept"
    assert bundle["usage_records"] == ["usage-1"]
    assert str(result["contentDigest"]).startswith("sha256:")
