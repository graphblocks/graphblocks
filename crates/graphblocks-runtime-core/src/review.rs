use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Value, json};

use crate::evaluation::{ResourceSnapshotRef, ReviewDecision, ReviewRecord};
use crate::policy::PrincipalRef;

#[derive(Clone, Debug, PartialEq)]
pub struct ReviewRequest {
    pub request_id: String,
    pub subject: ResourceSnapshotRef,
    pub requested_by: PrincipalRef,
    pub required_scopes: Vec<String>,
    pub created_at: String,
    pub metadata: BTreeMap<String, Value>,
}

impl ReviewRequest {
    pub fn new(
        request_id: impl Into<String>,
        subject: ResourceSnapshotRef,
        requested_by: PrincipalRef,
        required_scopes: impl IntoIterator<Item = impl Into<String>>,
        created_at: impl Into<String>,
    ) -> Self {
        let mut required_scopes = required_scopes
            .into_iter()
            .map(Into::into)
            .collect::<Vec<_>>();
        required_scopes.sort();
        required_scopes.dedup();
        Self {
            request_id: request_id.into(),
            subject,
            requested_by,
            required_scopes,
            created_at: created_at.into(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: Value) -> Self {
        self.metadata.insert(key.into(), value);
        self
    }

    pub fn content_digest(&self) -> String {
        canonical_hash(&json!({
            "subject": {
                "resource_id": self.subject.resource_id,
                "digest": self.subject.digest,
                "resource_kind": self.subject.resource_kind,
                "uri": self.subject.uri,
                "metadata": self.subject.metadata,
            },
            "requested_by": {
                "principal_id": self.requested_by.principal_id,
                "tenant_id": self.requested_by.tenant_id,
                "groups": self.requested_by.groups,
                "roles": self.requested_by.roles,
                "attributes": self.requested_by.attributes,
            },
            "required_scopes": self.required_scopes,
            "metadata": self.metadata,
        }))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ReviewerCredential {
    pub credential_ref: String,
    pub reviewer: PrincipalRef,
    pub scopes: Vec<String>,
    pub issued_at: String,
    pub expires_at: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl ReviewerCredential {
    pub fn new(
        credential_ref: impl Into<String>,
        reviewer: PrincipalRef,
        scopes: impl IntoIterator<Item = impl Into<String>>,
        issued_at: impl Into<String>,
    ) -> Self {
        let mut scopes = scopes.into_iter().map(Into::into).collect::<Vec<_>>();
        scopes.sort();
        scopes.dedup();
        Self {
            credential_ref: credential_ref.into(),
            reviewer,
            scopes,
            issued_at: issued_at.into(),
            expires_at: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn allows(&self, reviewer: &PrincipalRef, scope: &str) -> bool {
        self.reviewer.principal_id == reviewer.principal_id
            && self.scopes.iter().any(|item| item == scope)
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryReviewerCredentialProvider {
    pub credentials: Vec<ReviewerCredential>,
}

impl InMemoryReviewerCredentialProvider {
    pub fn new(credentials: impl IntoIterator<Item = ReviewerCredential>) -> Self {
        Self {
            credentials: credentials.into_iter().collect(),
        }
    }

    pub fn credentials_for(&self, reviewer: &PrincipalRef, scope: &str) -> Vec<ReviewerCredential> {
        self.credentials
            .iter()
            .filter(|credential| credential.allows(reviewer, scope))
            .cloned()
            .collect()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ReviewWorkflowError {
    SubjectChanged {
        expected_digest: String,
        actual_digest: String,
    },
    CredentialMissing {
        reviewer_id: String,
        scope: String,
    },
    ScopeNotRequested {
        scope: String,
    },
}

impl fmt::Display for ReviewWorkflowError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SubjectChanged {
                expected_digest,
                actual_digest,
            } => write!(
                formatter,
                "review subject changed: expected {expected_digest}, got {actual_digest}"
            ),
            Self::CredentialMissing { reviewer_id, scope } => {
                write!(
                    formatter,
                    "reviewer {reviewer_id:?} has no credential for scope {scope:?}"
                )
            }
            Self::ScopeNotRequested { scope } => {
                write!(formatter, "review scope {scope:?} was not requested")
            }
        }
    }
}

impl Error for ReviewWorkflowError {}

#[derive(Clone, Debug, PartialEq)]
pub struct ReviewWorkflow {
    pub request: ReviewRequest,
    pub credential_provider: InMemoryReviewerCredentialProvider,
    pub reviews: Vec<ReviewRecord>,
}

impl ReviewWorkflow {
    pub fn new(
        request: ReviewRequest,
        credential_provider: InMemoryReviewerCredentialProvider,
    ) -> Self {
        Self {
            request,
            credential_provider,
            reviews: Vec::new(),
        }
    }

    pub fn with_review(mut self, review: ReviewRecord) -> Self {
        self.reviews
            .retain(|existing| existing.review_id != review.review_id);
        self.reviews.push(review);
        self
    }

    pub fn record_review(
        &mut self,
        review_id: impl Into<String>,
        reviewer: PrincipalRef,
        scope: impl Into<String>,
        decision: ReviewDecision,
        created_at: impl Into<String>,
        subject: Option<ResourceSnapshotRef>,
        comments: impl IntoIterator<Item = impl Into<String>>,
    ) -> Result<ReviewRecord, ReviewWorkflowError> {
        let subject = subject.unwrap_or_else(|| self.request.subject.clone());
        let scope = scope.into();
        if subject.resource_id != self.request.subject.resource_id
            || subject.digest != self.request.subject.digest
        {
            return Err(ReviewWorkflowError::SubjectChanged {
                expected_digest: self.request.subject.digest.clone(),
                actual_digest: subject.digest,
            });
        }
        if !self
            .request
            .required_scopes
            .iter()
            .any(|item| item == &scope)
        {
            return Err(ReviewWorkflowError::ScopeNotRequested { scope });
        }
        let credentials = self.credential_provider.credentials_for(&reviewer, &scope);
        if credentials.is_empty() {
            return Err(ReviewWorkflowError::CredentialMissing {
                reviewer_id: reviewer.principal_id,
                scope,
            });
        }
        let mut review = ReviewRecord::new(
            review_id,
            self.request.subject.clone(),
            self.request.subject.digest.clone(),
            &scope,
            reviewer,
            decision,
        )
        .with_created_at(created_at);
        review.comments = comments.into_iter().map(Into::into).collect();
        review.credential_refs = credentials
            .iter()
            .map(|credential| credential.credential_ref.clone())
            .collect();
        self.reviews.push(review.clone());
        Ok(review)
    }

    pub fn completed_scopes(&self) -> Vec<String> {
        self.request
            .required_scopes
            .iter()
            .filter(|scope| {
                self.reviews.iter().any(|review| {
                    &review.scope == *scope
                        && matches!(
                            review.decision,
                            ReviewDecision::Accept | ReviewDecision::AcceptWithConditions
                        )
                        && review.is_valid_for(&self.request.subject)
                })
            })
            .cloned()
            .collect()
    }

    pub fn is_complete(&self) -> bool {
        self.completed_scopes() == self.request.required_scopes
    }
}
