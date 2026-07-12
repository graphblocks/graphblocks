from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks.evaluation import ResourceSnapshotRef
from graphblocks.policy import PrincipalRef


ROOT = Path(__file__).parents[1]


def test_review_package_preserves_retired_facade_wildcard_exports() -> None:
    graphblocks_review = importlib.import_module("graphblocks.review")
    exported: dict[str, object] = {}

    exec("from graphblocks.review import *", exported)

    expected_exports = {
        "PrincipalRef",
        "ResourceSnapshotRef",
        "ReviewDecision",
        "ReviewRecord",
    }
    assert expected_exports <= set(graphblocks_review.__all__)
    for export_name in expected_exports:
        assert exported[export_name] is getattr(graphblocks_review, export_name)


def test_review_package_reexports_workflow_contracts(monkeypatch) -> None:
    graphblocks_review = importlib.import_module("graphblocks.review")

    reviewer = PrincipalRef("reviewer-1")
    request = graphblocks_review.ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    workflow = graphblocks_review.ReviewWorkflow(
        request=request,
        credential_provider=graphblocks_review.InMemoryReviewerCredentialProvider(
            [
                graphblocks_review.ReviewerCredential(
                    "cred-quality",
                    reviewer,
                    scopes=("quality",),
                    issued_at="2026-06-24T00:00:00Z",
                )
            ]
        ),
    )

    review = workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        created_at="2026-06-24T00:05:00Z",
    )

    assert review.credential_refs == ["cred-quality"]
