from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import pickle
from threading import Barrier, BrokenBarrierError

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


def test_review_workflow_rechecks_provider_credential_identity_and_scope() -> None:
    request = ReviewRequest(
        request_id="request-credential-boundary",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    requested_reviewer = PrincipalRef("reviewer-1")
    unrelated_credential = ReviewerCredential(
        "cred-unrelated",
        PrincipalRef("reviewer-2"),
        scopes=("safety",),
        issued_at="2026-06-24T00:00:00Z",
    )

    class MisbehavingProvider:
        def credentials_for(
            self,
            reviewer: PrincipalRef,
            scope: str,
        ) -> tuple[ReviewerCredential, ...]:
            return (unrelated_credential,)

    workflow = ReviewWorkflow(request, MisbehavingProvider())
    with pytest.raises(ReviewCredentialMissingError):
        workflow.record_review(
            review_id="review-1",
            reviewer=requested_reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:01:00Z",
        )


def test_review_workflow_rejects_restored_reviews_outside_request() -> None:
    request = ReviewRequest(
        request_id="request-restore",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:01:00Z",
    )
    restored = ReviewRecord(
        review_id="review-1",
        subject=request.subject,
        subject_digest=request.subject.digest,
        scope="quality",
        reviewer=PrincipalRef("reviewer-1"),
        decision="accept",
        created_at="2026-06-24T00:00:59Z",
    )

    with pytest.raises(ValueError, match="must not precede request"):
        ReviewWorkflow(
            request,
            InMemoryReviewerCredentialProvider(),
            reviews=(restored,),
        )


def test_review_request_rejects_invalid_created_at() -> None:
    with pytest.raises(ValueError, match="review request created_at must be an ISO datetime"):
        ReviewRequest(
            request_id="request-1",
            subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
            requested_by=PrincipalRef("author-1"),
            required_scopes=("quality",),
            created_at="later",
        )


def test_review_request_rejects_invalid_identity_and_metadata_fields() -> None:
    base = {
        "request_id": "request-1",
        "subject": ResourceSnapshotRef("candidate-1", "sha256:subject"),
        "requested_by": PrincipalRef("author-1"),
        "required_scopes": ("quality",),
        "created_at": "2026-06-24T00:00:00Z",
    }

    cases = [
        ({"request_id": " "}, "review request request_id must not be empty"),
        ({"subject": object()}, "review request subject must be a ResourceSnapshotRef"),
        ({"requested_by": object()}, "review request requested_by must be a PrincipalRef"),
        ({"required_scopes": "quality"}, "review request required_scopes must be a collection of strings"),
        ({"required_scopes": ("quality", object())}, "review request required_scopes items must be strings"),
        ({"required_scopes": ("quality", " ")}, "review request required_scopes item must not be empty"),
        ({"metadata": object()}, "review request metadata must be a mapping"),
        ({"metadata": {object(): "value"}}, "review request metadata keys must be strings"),
        ({"metadata": {" ": "value"}}, "review request metadata key must not be empty"),
    ]

    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            ReviewRequest(**(base | overrides))  # type: ignore[arg-type]

    metadata = {"purpose": "release", "scope": {"labels": ["quality"]}}
    request = ReviewRequest(**(base | {"metadata": metadata}))
    metadata["purpose"] = "mutated"
    metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert request.metadata == {"purpose": "release", "scope": {"labels": ("quality",)}}
    with pytest.raises(TypeError):
        request.metadata["purpose"] = "mutated"
    with pytest.raises(TypeError):
        request.metadata["scope"]["labels"] = ("mutated",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        request.metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]


def test_review_evidence_snapshots_support_standard_serialization() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
        metadata={"scope": {"labels": ["quality"]}},
    )

    assert json.loads(json.dumps(asdict(request)))["metadata"] == {
        "scope": {"labels": ["quality"]}
    }
    assert pickle.loads(pickle.dumps(request)) == request


def test_review_request_rejects_non_json_and_cyclic_metadata_values() -> None:
    base = {
        "request_id": "request-1",
        "subject": ResourceSnapshotRef("candidate-1", "sha256:subject"),
        "requested_by": PrincipalRef("author-1"),
        "required_scopes": ("quality",),
        "created_at": "2026-06-22T00:00:00Z",
    }
    with pytest.raises(
        ValueError,
        match="review request metadata must contain strict canonical JSON",
    ):
        ReviewRequest(**(base | {"metadata": {"invalid": object()}}))  # type: ignore[arg-type]

    metadata: dict[str, object] = {}
    metadata["self"] = metadata
    with pytest.raises(ValueError, match="metadata must not contain cyclic values"):
        ReviewRequest(**(base | {"metadata": metadata}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        (
            {"request_id": " request-1"},
            "review request request_id must not contain surrounding whitespace",
        ),
        (
            {"required_scopes": (" quality",)},
            "review request required_scopes item must not contain surrounding whitespace",
        ),
        (
            {"metadata": {" purpose": "release"}},
            "review request metadata key must not contain surrounding whitespace",
        ),
        (
            {"metadata": {"scope": {" label": "quality"}}},
            "review request metadata key must not contain surrounding whitespace",
        ),
    ),
)
def test_review_request_rejects_whitespace_wrapped_identities(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    base = {
        "request_id": "request-1",
        "subject": ResourceSnapshotRef("candidate-1", "sha256:subject"),
        "requested_by": PrincipalRef("author-1"),
        "required_scopes": ("quality",),
        "created_at": "2026-06-24T00:00:00Z",
    }

    with pytest.raises(ValueError, match=expected_error):
        ReviewRequest(**(base | overrides))  # type: ignore[arg-type]


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


def test_review_workflow_rejects_malformed_injected_state_and_provider_results() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")

    with pytest.raises(ValueError, match="credentials must be ReviewerCredential"):
        InMemoryReviewerCredentialProvider([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="request must be a ReviewRequest"):
        ReviewWorkflow(object(), InMemoryReviewerCredentialProvider())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must provide credentials_for"):
        ReviewWorkflow(request, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reviews must be ReviewRecord"):
        ReviewWorkflow(
            request,
            InMemoryReviewerCredentialProvider(),
            reviews=(object(),),  # type: ignore[arg-type]
        )

    class InvalidCredentialProvider:
        def credentials_for(self, reviewer: PrincipalRef, scope: str) -> tuple[object, ...]:
            return (object(),)

    workflow = ReviewWorkflow(request, InvalidCredentialProvider())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="review must be a ReviewRecord"):
        workflow.with_review(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="review reviewer must be a PrincipalRef"):
        workflow.record_review(
            review_id="review-1",
            reviewer=object(),  # type: ignore[arg-type]
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )
    with pytest.raises(ValueError, match="scope must not contain surrounding whitespace"):
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope=" quality",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )
    with pytest.raises(ValueError, match="must return ReviewerCredential"):
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )


def test_review_workflow_replaces_replayed_review_id() -> None:
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
            [
                ReviewerCredential(
                    "cred-quality",
                    reviewer,
                    scopes=("quality",),
                    issued_at="2026-06-24T00:00:00Z",
                )
            ]
        ),
    )
    workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="reject",
        created_at="2026-06-24T00:05:00Z",
    )

    replayed = workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        created_at="2026-06-24T00:06:00Z",
    )

    assert workflow.reviews == (replayed,)
    assert workflow.completed_scopes() == ("quality",)


def test_review_workflow_serializes_concurrent_review_recording() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality", "safety"),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider(
            [
                ReviewerCredential(
                    "cred-reviewer",
                    reviewer,
                    scopes=("quality", "safety"),
                    issued_at="2026-06-24T00:00:00Z",
                )
            ]
        ),
    )
    reads = Barrier(2)

    class CoordinatedReviews(tuple[ReviewRecord, ...]):
        def __iter__(self):
            try:
                reads.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            return super().__iter__()

    workflow.reviews = CoordinatedReviews()

    def record(scope: str) -> None:
        workflow.record_review(
            review_id=f"review-{scope}",
            reviewer=reviewer,
            scope=scope,
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(record, ("quality", "safety")))

    assert {review.review_id for review in workflow.reviews} == {
        "review-quality",
        "review-safety",
    }
    assert workflow.is_complete()


def test_reviewer_credential_rejects_invalid_timestamps() -> None:
    reviewer = PrincipalRef("reviewer-1")

    with pytest.raises(ValueError, match="reviewer credential issued_at must be an ISO datetime"):
        ReviewerCredential("cred-quality", reviewer, scopes=("quality",), issued_at="later")
    with pytest.raises(ValueError, match="reviewer credential expires_at must be an ISO datetime"):
        ReviewerCredential(
            "cred-quality",
            reviewer,
            scopes=("quality",),
            issued_at="2026-06-24T00:00:00Z",
            expires_at="later",
        )
    with pytest.raises(ValueError, match="reviewer credential expires_at must be after issued_at"):
        ReviewerCredential(
            "cred-quality",
            reviewer,
            scopes=("quality",),
            issued_at="2026-06-24T00:00:00Z",
            expires_at="2026-06-24T00:00:00Z",
        )


def test_review_timestamps_reject_non_rfc3339_forms() -> None:
    subject = ResourceSnapshotRef("candidate-1", "sha256:subject")
    author = PrincipalRef("author-1")
    reviewer = PrincipalRef("reviewer-1")
    request_base = {
        "request_id": "request-1",
        "subject": subject,
        "requested_by": author,
        "required_scopes": ("quality",),
    }
    credential_base = {
        "credential_ref": "cred-quality",
        "reviewer": reviewer,
        "scopes": ("quality",),
        "issued_at": "2026-06-24T00:00:00Z",
    }

    for created_at in (
        "2026-06-24 00:00:00Z",
        "2026-06-24T00:00:00",
        "2026-06-24T00:00:00+0000",
        "2026-06-24T00:00:00z",
        "2026-06-24T00:00:00Z ",
    ):
        with pytest.raises(ValueError, match="review request created_at must be an ISO datetime"):
            ReviewRequest(**(request_base | {"created_at": created_at}))  # type: ignore[arg-type]

    for issued_at in (
        "2026-06-24 00:00:00Z",
        "2026-06-24T00:00:00",
        "2026-06-24T00:00:00+0000",
        "2026-06-24T00:00:00z",
    ):
        with pytest.raises(ValueError, match="reviewer credential issued_at must be an ISO datetime"):
            ReviewerCredential(**(credential_base | {"issued_at": issued_at}))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reviewer credential expires_at must be an ISO datetime"):
        ReviewerCredential(**(credential_base | {"expires_at": "2026-06-24T00:05:00+0000"}))

    request = ReviewRequest(**(request_base | {"created_at": "2026-06-24T00:00:00Z"}))
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider([ReviewerCredential(**credential_base)]),
    )

    with pytest.raises(ValueError, match="review created_at must be an ISO datetime"):
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24 00:05:00Z",
        )


def test_reviewer_credential_rejects_invalid_identity_and_metadata_fields() -> None:
    reviewer = PrincipalRef("reviewer-1")
    base = {
        "credential_ref": "cred-quality",
        "reviewer": reviewer,
        "scopes": ("quality",),
        "issued_at": "2026-06-24T00:00:00Z",
    }

    cases = [
        ({"credential_ref": " "}, "reviewer credential credential_ref must not be empty"),
        ({"reviewer": object()}, "reviewer credential reviewer must be a PrincipalRef"),
        ({"scopes": "quality"}, "reviewer credential scopes must be a collection of strings"),
        ({"scopes": ("quality", object())}, "reviewer credential scopes items must be strings"),
        ({"scopes": ("quality", " ")}, "reviewer credential scopes item must not be empty"),
        ({"metadata": object()}, "reviewer credential metadata must be a mapping"),
        ({"metadata": {object(): "value"}}, "reviewer credential metadata keys must be strings"),
        ({"metadata": {" ": "value"}}, "reviewer credential metadata key must not be empty"),
    ]

    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            ReviewerCredential(**(base | overrides))  # type: ignore[arg-type]

    metadata = {"source": "policy", "scope": {"labels": ["quality"]}}
    credential = ReviewerCredential(**(base | {"metadata": metadata}))
    metadata["source"] = "mutated"
    metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert credential.metadata == {"source": "policy", "scope": {"labels": ("quality",)}}
    with pytest.raises(TypeError):
        credential.metadata["source"] = "mutated"
    with pytest.raises(TypeError):
        credential.metadata["scope"]["labels"] = ("mutated",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        credential.metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]
    assert credential.scopes == ("quality",)


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        (
            {"credential_ref": " cred-quality"},
            "reviewer credential credential_ref must not contain surrounding whitespace",
        ),
        (
            {"scopes": (" quality",)},
            "reviewer credential scopes item must not contain surrounding whitespace",
        ),
        (
            {"metadata": {" source": "policy"}},
            "reviewer credential metadata key must not contain surrounding whitespace",
        ),
        (
            {"metadata": {"scope": {" label": "quality"}}},
            "reviewer credential metadata key must not contain surrounding whitespace",
        ),
    ),
)
def test_reviewer_credential_rejects_whitespace_wrapped_identities(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    base = {
        "credential_ref": "cred-quality",
        "reviewer": PrincipalRef("reviewer-1"),
        "scopes": ("quality",),
        "issued_at": "2026-06-24T00:00:00Z",
    }

    with pytest.raises(ValueError, match=expected_error):
        ReviewerCredential(**(base | overrides))  # type: ignore[arg-type]


def test_review_workflow_rejects_invalid_review_created_at() -> None:
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

    with pytest.raises(ValueError, match="review created_at must be an ISO datetime"):
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="later",
        )


def test_review_workflow_rejects_review_before_request_creation() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:05:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider(
            [ReviewerCredential("cred-quality", reviewer, scopes=("quality",), issued_at="2026-06-24T00:00:00Z")]
        ),
    )

    with pytest.raises(ValueError, match="review created_at must not precede request created_at"):
        workflow.record_review(
            review_id="review-1",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:04:59Z",
        )

    assert workflow.reviews == ()


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


def test_review_workflow_rejects_credential_from_different_tenant() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1", tenant_id="tenant-a"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    credential_reviewer = PrincipalRef("reviewer-1", tenant_id="tenant-a")
    submitted_reviewer = PrincipalRef("reviewer-1", tenant_id="tenant-b")
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider(
            [
                ReviewerCredential(
                    "cred-quality",
                    credential_reviewer,
                    scopes=("quality",),
                    issued_at="2026-06-24T00:00:00Z",
                )
            ]
        ),
    )

    with pytest.raises(ReviewCredentialMissingError):
        workflow.record_review(
            review_id="review-1",
            reviewer=submitted_reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )


def test_review_workflow_ignores_expired_credentials() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    provider = InMemoryReviewerCredentialProvider(
        [
            ReviewerCredential(
                "cred-expired",
                reviewer,
                scopes=("quality",),
                issued_at="2026-06-23T00:00:00Z",
                expires_at="2026-06-24T00:04:59Z",
            ),
            ReviewerCredential(
                "cred-valid-offset",
                reviewer,
                scopes=("quality",),
                issued_at="2026-06-23T00:00:00Z",
                expires_at="2026-06-23T19:06:00-05:00",
            ),
        ]
    )
    workflow = ReviewWorkflow(request=request, credential_provider=provider)

    review = workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        created_at="2026-06-24T00:05:00Z",
    )

    assert review.credential_refs == ["cred-valid-offset"]

    expired_workflow = ReviewWorkflow(request=request, credential_provider=provider)
    with pytest.raises(ReviewCredentialMissingError) as error:
        expired_workflow.record_review(
            review_id="review-2",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:06:01Z",
        )

    assert error.value.reviewer == reviewer
    assert error.value.scope == "quality"


def test_review_workflow_ignores_not_yet_issued_credentials() -> None:
    request = ReviewRequest(
        request_id="request-1",
        subject=ResourceSnapshotRef("candidate-1", "sha256:subject"),
        requested_by=PrincipalRef("author-1"),
        required_scopes=("quality",),
        created_at="2026-06-24T00:00:00Z",
    )
    reviewer = PrincipalRef("reviewer-1")
    current = ReviewerCredential(
        "cred-current",
        reviewer,
        scopes=("quality",),
        issued_at="2026-06-24T00:00:00Z",
    )
    future = ReviewerCredential(
        "cred-future",
        reviewer,
        scopes=("quality",),
        issued_at="2026-06-24T00:10:00Z",
    )
    workflow = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider([future, current]),
    )

    review = workflow.record_review(
        review_id="review-1",
        reviewer=reviewer,
        scope="quality",
        decision="accept",
        created_at="2026-06-24T00:05:00Z",
    )

    assert review.credential_refs == ["cred-current"]

    future_only = ReviewWorkflow(
        request=request,
        credential_provider=InMemoryReviewerCredentialProvider([future]),
    )
    with pytest.raises(ReviewCredentialMissingError):
        future_only.record_review(
            review_id="review-2",
            reviewer=reviewer,
            scope="quality",
            decision="accept",
            created_at="2026-06-24T00:05:00Z",
        )


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
