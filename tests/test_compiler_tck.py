from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphblocks import compile_graph
from graphblocks.plugins import BlockCatalog


AMENDMENT_COMPILER_DIAGNOSTICS = {
    "ToolBindingMissing",
    "ToolSchemaMissing",
    "ApprovalWithoutArgumentDigest",
    "UnsafeParallelEffects",
    "NonIdempotentRetry",
    "OutputPolicyBypass",
    "ImmediateDraftWithoutRetractionSupport",
    "PolicyGateAfterDelivery",
    "PendingToolCallAfterAbort",
    "CommitAfterPolicyStop",
    "UnboundedPolicyHoldback",
    "GB6001",
    "GB6002",
    "GB6003",
    "GB6004",
    "GB6005",
    "GB6006",
    "GB6007",
    "GB6008",
    "GB6009",
    "GB6010",
    "GB6011",
    "GB6012",
    "GB6013",
    "GB6014",
    "GB6015",
    "GB6016",
}


def test_python_compiler_matches_shared_tck_cases() -> None:
    cases = json.loads((Path(__file__).parents[1] / "tck" / "compiler" / "cases.json").read_text())

    for case in cases:
        block_catalog = None
        if "block_catalog" in case:
            block_catalog = BlockCatalog.from_blocks(case["block_catalog"])
        plan = compile_graph(case["document"], block_catalog=block_catalog)
        error_codes = [
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"
        ]
        warning_codes = [
            diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "warning"
        ]

        expected: dict[str, Any] = case["expected"]
        assert plan.graph_hash == expected["graph_hash"], case["name"]
        assert error_codes == expected["error_codes"], case["name"]
        assert warning_codes == expected.get("warning_codes", []), case["name"]


def test_compiler_tck_covers_tool_output_policy_amendment_diagnostics() -> None:
    cases = json.loads((Path(__file__).parents[1] / "tck" / "compiler" / "cases.json").read_text())
    covered_codes = {
        code
        for case in cases
        for code in case.get("expected", {}).get("error_codes", [])
        if isinstance(code, str)
    }

    assert sorted(AMENDMENT_COMPILER_DIAGNOSTICS - covered_codes) == []


def test_compiler_accepts_async_start_absolute_expiration_as_wait_bound() -> None:
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "async-start-absolute-expiration"},
            "spec": {
                "nodes": {
                    "startCI": {
                        "block": "async.start_operation@1",
                        "config": {
                            "operationId": "op-ci-absolute",
                            "runId": "run-coding-1",
                            "nodeId": "startCI",
                            "attemptId": "attempt-1",
                            "kind": "ci_job",
                            "providerOperationId": "gha-run-1",
                            "resumeTokenHash": "sha256:resume-token",
                            "idempotencyKey": "idem-op-ci-absolute",
                            "expectedSchema": "schemas/CICallback@1",
                            "createdAtUnixMs": 1_000,
                            "submittedAtUnixMs": 1_050,
                            "expiresAtUnixMs": 1_801_000,
                            "callback": {"required": True, "schema": "schemas/CICallback@1"},
                            "resume": {
                                "requirePolicyReevaluation": True,
                                "requireBudgetReservation": True,
                                "requireReleaseCompatibility": True,
                                "requireOwnershipFence": True,
                            },
                            "attemptFencing": True,
                        },
                    }
                }
            },
        }
    )

    error_codes = [diagnostic.code for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"]

    assert "GB6001" not in error_codes
    assert "InvalidAsyncOperation" not in error_codes


def test_compiler_rejects_async_start_absolute_and_relative_wait_bounds() -> None:
    plan = compile_graph(
        {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "async-start-ambiguous-expiration"},
            "spec": {
                "nodes": {
                    "startCI": {
                        "block": "async.start_operation@1",
                        "config": {
                            "operationId": "op-ci-ambiguous",
                            "runId": "run-coding-1",
                            "nodeId": "startCI",
                            "attemptId": "attempt-1",
                            "kind": "ci_job",
                            "providerOperationId": "gha-run-1",
                            "resumeTokenHash": "sha256:resume-token",
                            "idempotencyKey": "idem-op-ci-ambiguous",
                            "expectedSchema": "schemas/CICallback@1",
                            "createdAtUnixMs": 1_000,
                            "submittedAtUnixMs": 1_050,
                            "expiresAtUnixMs": 1_801_000,
                            "timeoutMs": 1_800_000,
                            "callback": {"required": True, "schema": "schemas/CICallback@1"},
                            "resume": {
                                "requirePolicyReevaluation": True,
                                "requireBudgetReservation": True,
                                "requireReleaseCompatibility": True,
                                "requireOwnershipFence": True,
                            },
                            "attemptFencing": True,
                        },
                    }
                }
            },
        }
    )

    errors = [diagnostic for diagnostic in plan.diagnostics.diagnostics if diagnostic.severity == "error"]

    assert [diagnostic.code for diagnostic in errors] == ["InvalidAsyncOperation"]
    assert "must not define both expiresAtUnixMs and timeout" in errors[0].message
