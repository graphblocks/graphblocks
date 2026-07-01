#![allow(clippy::panic)]

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use graphblocks_runtime_core::evaluation::{ResourceSnapshotRef, ReviewDecision};
use graphblocks_runtime_core::policy::PrincipalRef;
use graphblocks_runtime_core::review::{
    InMemoryReviewerCredentialProvider, ReviewRequest, ReviewSubmission, ReviewWorkflow,
    ReviewWorkflowError, ReviewerCredential,
};
use serde_json::{Map, Value, json};

#[test]
fn approval_review_tck_cases_match_runtime_core() {
    let mut fixture_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    fixture_path.push("../../tck/approval-review/cases.json");
    let raw_fixture =
        fs::read_to_string(&fixture_path).expect("approval-review fixture is readable");
    let cases: Vec<Value> =
        serde_json::from_str(&raw_fixture).expect("approval-review fixture is valid");

    let required_str = |mapping: &Map<String, Value>, keys: &[&str]| -> String {
        for key in keys {
            if let Some(value) = mapping.get(*key).and_then(Value::as_str) {
                return value.to_owned();
            }
        }
        panic!("missing required string field {keys:?}");
    };
    let optional_str = |mapping: &Map<String, Value>, keys: &[&str]| -> Option<String> {
        keys.iter()
            .find_map(|key| mapping.get(*key).and_then(Value::as_str))
            .map(str::to_owned)
    };
    let strings = |value: Option<&Value>| -> Vec<String> {
        match value {
            Some(Value::Array(items)) => items
                .iter()
                .map(|item| item.as_str().expect("string list item").to_owned())
                .collect(),
            Some(Value::String(item)) => vec![item.to_owned()],
            _ => Vec::new(),
        }
    };
    let metadata = |mapping: &Map<String, Value>| -> BTreeMap<String, Value> {
        mapping
            .get("metadata")
            .and_then(Value::as_object)
            .map(|metadata| {
                metadata
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect()
            })
            .unwrap_or_default()
    };
    let subject_from = |mapping: &Map<String, Value>| -> ResourceSnapshotRef {
        ResourceSnapshotRef {
            resource_id: required_str(mapping, &["resourceId", "resource_id"]),
            digest: required_str(mapping, &["digest"]),
            resource_kind: optional_str(mapping, &["resourceKind", "resource_kind"]),
            uri: optional_str(mapping, &["uri"]),
            metadata: metadata(mapping),
        }
    };
    let principal_from = |mapping: &Map<String, Value>| -> PrincipalRef {
        PrincipalRef {
            principal_id: required_str(mapping, &["principalId", "principal_id"]),
            tenant_id: optional_str(mapping, &["tenantId", "tenant_id"]),
            groups: strings(mapping.get("groups")),
            roles: strings(mapping.get("roles")),
            attributes: mapping
                .get("attributes")
                .and_then(Value::as_object)
                .map(|attributes| {
                    attributes
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone()))
                        .collect()
                })
                .unwrap_or_default(),
        }
    };
    let decision_from = |value: &str| -> ReviewDecision {
        match value {
            "accept" => ReviewDecision::Accept,
            "accept_with_conditions" => ReviewDecision::AcceptWithConditions,
            "revise" => ReviewDecision::Revise,
            "reject" => ReviewDecision::Reject,
            other => panic!("unsupported review decision {other:?}"),
        }
    };

    for raw_case in cases {
        let case = raw_case
            .as_object()
            .expect("approval-review case is object");
        let name = required_str(case, &["name", "case_id", "caseId"]);
        let kind = required_str(case, &["kind"]);
        let subject = subject_from(
            case.get("subject")
                .and_then(Value::as_object)
                .expect("case subject"),
        );
        let requested_by = principal_from(
            case.get("requestedBy")
                .or_else(|| case.get("requested_by"))
                .and_then(Value::as_object)
                .expect("case requestedBy"),
        );
        let required_scopes = strings(
            case.get("requiredScopes")
                .or_else(|| case.get("required_scopes")),
        );
        let request = ReviewRequest::new(
            case.get("requestId")
                .or_else(|| case.get("request_id"))
                .and_then(Value::as_str)
                .unwrap_or("request-1"),
            subject.clone(),
            requested_by.clone(),
            required_scopes,
            required_str(case, &["createdAt", "created_at"]),
        );

        let observed = if kind == "review_digest" {
            let reordered_request = ReviewRequest::new(
                "request-reordered",
                subject.clone(),
                requested_by.clone(),
                strings(
                    case.get("reorderedScopes")
                        .or_else(|| case.get("reordered_scopes")),
                ),
                required_str(case, &["createdAt", "created_at"]),
            );
            let changed_subject = subject_from(
                case.get("changedSubject")
                    .or_else(|| case.get("changed_subject"))
                    .and_then(Value::as_object)
                    .expect("case changedSubject"),
            );
            let changed_request = ReviewRequest::new(
                "request-changed",
                changed_subject,
                requested_by,
                reordered_request.required_scopes.clone(),
                required_str(case, &["createdAt", "created_at"]),
            );
            json!({
                "sameDigest": request.content_digest() == reordered_request.content_digest(),
                "changedDigestDifferent": request.content_digest() != changed_request.content_digest(),
                "requiredScopes": request.required_scopes,
            })
        } else {
            let reviewer_mapping = case
                .get("reviewer")
                .and_then(Value::as_object)
                .expect("case reviewer");
            let reviewer = principal_from(reviewer_mapping);
            let raw_credentials = case
                .get("credentials")
                .and_then(Value::as_array)
                .expect("case credentials");
            let mut credentials = Vec::new();
            for raw_credential in raw_credentials {
                let credential_mapping = raw_credential.as_object().expect("credential object");
                let credential_reviewer = credential_mapping
                    .get("reviewer")
                    .and_then(Value::as_object)
                    .map(&principal_from)
                    .unwrap_or_else(|| reviewer.clone());
                let mut credential = ReviewerCredential::new(
                    credential_mapping
                        .get("credentialRef")
                        .or_else(|| credential_mapping.get("credential_ref"))
                        .and_then(Value::as_str)
                        .expect("credentialRef"),
                    credential_reviewer,
                    strings(credential_mapping.get("scopes")),
                    credential_mapping
                        .get("issuedAt")
                        .or_else(|| credential_mapping.get("issued_at"))
                        .and_then(Value::as_str)
                        .expect("issuedAt"),
                );
                credential.expires_at =
                    optional_str(credential_mapping, &["expiresAt", "expires_at"]);
                credentials.push(credential);
            }
            let review_mapping = case
                .get("review")
                .and_then(Value::as_object)
                .expect("case review");
            let mut workflow = ReviewWorkflow::new(
                request.clone(),
                InMemoryReviewerCredentialProvider::new(credentials),
            );
            let review_id = review_mapping
                .get("reviewId")
                .or_else(|| review_mapping.get("review_id"))
                .and_then(Value::as_str)
                .unwrap_or("review-1");
            let scope = required_str(review_mapping, &["scope"]);
            let decision = decision_from(&required_str(review_mapping, &["decision"]));
            let created_at = required_str(review_mapping, &["createdAt", "created_at"]);
            let comments = strings(review_mapping.get("comments"));

            match kind.as_str() {
                "review_record" => {
                    let review = workflow
                        .record_review(
                            ReviewSubmission::new(review_id, reviewer, scope, decision, created_at)
                                .with_comments(comments),
                        )
                        .expect("review record is accepted");
                    json!({
                        "credentialRefs": review.credential_refs,
                        "completedScopes": workflow.completed_scopes(),
                        "complete": workflow.is_complete(),
                        "validForSubject": review.is_valid_for(&subject),
                    })
                }
                "review_changed_subject" => {
                    let changed_subject = subject_from(
                        case.get("changedSubject")
                            .or_else(|| case.get("changed_subject"))
                            .and_then(Value::as_object)
                            .expect("case changedSubject"),
                    );
                    match workflow.record_review(
                        ReviewSubmission::new(review_id, reviewer, scope, decision, created_at)
                            .with_subject(changed_subject)
                            .with_comments(comments),
                    ) {
                        Err(ReviewWorkflowError::SubjectChanged {
                            expected_digest,
                            actual_digest,
                        }) => json!({
                            "error": "review_subject_changed",
                            "expectedDigest": expected_digest,
                            "actualDigest": actual_digest,
                        }),
                        other => panic!("{name} expected subject changed error, got {other:?}"),
                    }
                }
                "review_invalidated" => {
                    let review = workflow
                        .record_review(
                            ReviewSubmission::new(review_id, reviewer, scope, decision, created_at)
                                .with_comments(comments),
                        )
                        .expect("review record is accepted");
                    let invalidated_at =
                        required_str(review_mapping, &["invalidatedAt", "invalidated_at"]);
                    let workflow = workflow.with_review(review.invalidate(invalidated_at));
                    json!({
                        "completedScopes": workflow.completed_scopes(),
                        "complete": workflow.is_complete(),
                    })
                }
                "review_missing_credential" => match workflow.record_review(
                    ReviewSubmission::new(review_id, reviewer, scope, decision, created_at)
                        .with_comments(comments),
                ) {
                    Err(ReviewWorkflowError::CredentialMissing { reviewer_id, scope }) => json!({
                        "error": "review_credential_missing",
                        "reviewerId": reviewer_id,
                        "scope": scope,
                    }),
                    other => panic!("{name} expected missing credential error, got {other:?}"),
                },
                other => panic!("unsupported approval-review case kind {other:?}"),
            }
        };

        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .expect("expected object");
        for (key, expected_value) in expected {
            assert_eq!(
                observed.get(key),
                Some(expected_value),
                "{name} expected {key}"
            );
        }
    }
}
