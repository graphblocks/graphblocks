from __future__ import annotations

import pytest

from graphblocks.evaluation import ResourceSnapshotRef, ReviewRecord
from graphblocks.policy import PrincipalRef
from graphblocks.review import (
    InMemoryReviewerCredentialProvider,
    ReviewCredentialMissingError,
    ReviewRequest,
    ReviewSubjectChangedError,
    ReviewWorkflow,
    ReviewerCredential,
)


def test_review_request_digest_is_bound_to_subject_and_required_scopes() -> None:
    left = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("safety", "quality"),
        created_at="2026-06-24T00:00:00Z",
    )
    right = ReviewRequest(
        request_id="request-2",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality", "safety"),
        created_at="2026-06-24T00:01:00Z",
    )
    changed_subject = ReviewRequest(
        request_id="request-3",
        subject=ResourceSnapshotRef("candidate-1", "sha256:changed"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality", "safety"),
        created_at="2026-06-24T00:01:00Z",
    )

    assert left.content_digest() == right.content_digest()
    assert left.content_digest() != changed_subject.content_digest()
    assert left.required_scopes == ("quality", "safety")


def test_review_workflow_records_review_with_credential_reference() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1", roles=("qa",))
    provider = InMemoryReviewerCredentialProvider(
        [
            ReviewerCredential(
                credential_ref="cred-qa-1",
                reviewer=reviewer,
                scopes=("quality", "safety"),
                issued_at="2026-06-24T00:00:00Z",
            )
        ]
    )
    workflow = ReviewWorkflow(request=request, credential_provider=provider)

    review = workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        comments=["matches release criteria"],
        created_at="2026-06-24T00:05:00Z",
    )

    assert isinstance(review, ReviewRecord)
    assert review.subject_digest == "sha256:subject"
    assert review.credential_refs == ["cred-qa-1"]
    assert review.is_valid_for(request.subject)
    assert workflow.completed_scopes() == ("quality",)
    assert workflow.is_complete()


def test_review_workflow_rejects_missing_credential_for_scope() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("security",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider(
            [ReviewerCredential("cred-quality", reviewer, scopes=("quality",), issued_at="2026-06-24T00:00:00Z")]
        ),
    )

    with pytest.raises(ReviewCredentialMissingError) as error:
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="security",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )

    assert error.value.reviewer == reviewer
    assert error.value.scope == "security"


def test_review_workflow_rejects_changed_subject_and_ignores_invalidated_reviews() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider(
            [ReviewerCredential("cred-quality", reviewer, scopes=("quality",), issued_at="2026-06-24T00:00:00Z")]
        ),
    )

    with pytest.raises(ReviewSubjectChangedError) as error:
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            subject=ResourceSnapshotRef("candidate-1", "sha256:changed"),
            created_at="2026-06-24T00:05:00Z",
        )

    assert error.value.expected_digest == "sha256:subject"
    assert error.value.actual_digest == "sha256:changed"

    review = workflow.record_review(
        review_id="review-2",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        created_at="2026-06-24T00:06:00Z",
    )
    workflow = workflow.with_review(review.invalidate("2026-06-24T00:07:00Z"))

    assert workflow.completed_scopes() == ()
    assert not workflow.is_complete()
