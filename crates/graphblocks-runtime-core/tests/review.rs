use graphblocks_runtime_core::evaluation::{ResourceSnapshotRef, ReviewDecision};
use graphblocks_runtime_core::policy::PrincipalRef;
use graphblocks_runtime_core::review::{
    InMemoryReviewerCredentialProvider, ReviewRequest, ReviewWorkflow, ReviewWorkflowError,
    ReviewerCredential,
};

#[test]
fn review_request_digest_is_bound_to_subject_and_required_scopes() {
    let subject = ResourceSnapshotRef::new("candidate-1", "sha256:subject");
    let reviewer = PrincipalRef::new("author-1");
    let left = ReviewRequest::new(
        "request-1",
        subject.clone(),
        reviewer.clone(),
        ["safety", "quality"],
        "2026-06-24T00:00:00Z",
    );
    let right = ReviewRequest::new(
        "request-2",
        subject,
        reviewer.clone(),
        ["quality", "safety"],
        "2026-06-24T00:01:00Z",
    );
    let changed = ReviewRequest::new(
        "request-3",
        ResourceSnapshotRef::new("candidate-1", "sha256:changed"),
        reviewer,
        ["quality", "safety"],
        "2026-06-24T00:01:00Z",
    );

    assert_eq!(left.content_digest(), right.content_digest());
    assert_ne!(left.content_digest(), changed.content_digest());
    assert_eq!(left.required_scopes, vec!["quality", "safety"]);
}

#[test]
fn review_workflow_records_review_with_credential_reference() {
    let request = ReviewRequest::new(
        "request-1",
        ResourceSnapshotRef::new("candidate-1", "sha256:subject"),
        PrincipalRef::new("author-1"),
        ["quality"],
        "2026-06-24T00:00:00Z",
    );
    let reviewer = PrincipalRef::new("reviewer-1").with_role("qa");
    let provider = InMemoryReviewerCredentialProvider::new([ReviewerCredential::new(
        "cred-qa-1",
        reviewer.clone(),
        ["quality", "safety"],
        "2026-06-24T00:00:00Z",
    )]);
    let mut workflow = ReviewWorkflow::new(request.clone(), provider);

    let review = workflow
        .record_review(
            "review-1",
            reviewer,
            "quality",
            ReviewDecision::Accept,
            "2026-06-24T00:05:00Z",
            None,
            ["matches release criteria"],
        )
        .expect("review is accepted");

    assert_eq!(review.subject_digest, "sha256:subject");
    assert_eq!(review.credential_refs, vec!["cred-qa-1"]);
    assert!(review.is_valid_for(&request.subject));
    assert_eq!(workflow.completed_scopes(), vec!["quality"]);
    assert!(workflow.is_complete());
}

#[test]
fn review_workflow_rejects_changed_subject_and_ignores_invalidated_reviews() {
    let request = ReviewRequest::new(
        "request-1",
        ResourceSnapshotRef::new("candidate-1", "sha256:subject"),
        PrincipalRef::new("author-1"),
        ["quality"],
        "2026-06-24T00:00:00Z",
    );
    let reviewer = PrincipalRef::new("reviewer-1");
    let provider = InMemoryReviewerCredentialProvider::new([ReviewerCredential::new(
        "cred-quality",
        reviewer.clone(),
        ["quality"],
        "2026-06-24T00:00:00Z",
    )]);
    let mut workflow = ReviewWorkflow::new(request.clone(), provider);

    assert_eq!(
        workflow.record_review(
            "review-1",
            reviewer.clone(),
            "quality",
            ReviewDecision::Accept,
            "2026-06-24T00:05:00Z",
            Some(ResourceSnapshotRef::new("candidate-1", "sha256:changed")),
            Vec::<String>::new(),
        ),
        Err(ReviewWorkflowError::SubjectChanged {
            expected_digest: "sha256:subject".to_owned(),
            actual_digest: "sha256:changed".to_owned(),
        }),
    );

    let review = workflow
        .record_review(
            "review-2",
            reviewer,
            "quality",
            ReviewDecision::Accept,
            "2026-06-24T00:06:00Z",
            None,
            Vec::<String>::new(),
        )
        .expect("review is accepted")
        .invalidate("2026-06-24T00:07:00Z");
    let workflow = workflow.with_review(review);

    assert!(workflow.completed_scopes().is_empty());
    assert!(!workflow.is_complete());
}

#[test]
fn review_workflow_rejects_missing_credential_for_scope() {
    let request = ReviewRequest::new(
        "request-1",
        ResourceSnapshotRef::new("candidate-1", "sha256:subject"),
        PrincipalRef::new("author-1"),
        ["security"],
        "2026-06-24T00:00:00Z",
    );
    let reviewer = PrincipalRef::new("reviewer-1");
    let mut workflow = ReviewWorkflow::new(
        request,
        InMemoryReviewerCredentialProvider::new([ReviewerCredential::new(
            "cred-quality",
            reviewer.clone(),
            ["quality"],
            "2026-06-24T00:00:00Z",
        )]),
    );

    assert_eq!(
        workflow.record_review(
            "review-1",
            reviewer,
            "security",
            ReviewDecision::Accept,
            "2026-06-24T00:05:00Z",
            None,
            Vec::<String>::new(),
        ),
        Err(ReviewWorkflowError::CredentialMissing {
            reviewer_id: "reviewer-1".to_owned(),
            scope: "security".to_owned(),
        }),
    );
}
