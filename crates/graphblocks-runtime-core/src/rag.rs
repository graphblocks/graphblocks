use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use graphblocks_compiler::canonical::canonical_hash;
use serde_json::{Map, Value, json};

use crate::documents::{DocumentChunk, DocumentSpan, SourceRef};
use crate::evaluation::ResultBundle;

#[derive(Clone, Debug, PartialEq)]
pub struct SearchRequest {
    pub query_text: String,
    pub top_k: usize,
    pub filters: BTreeMap<String, Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl SearchRequest {
    pub fn new(query_text: impl Into<String>) -> Self {
        Self {
            query_text: query_text.into(),
            top_k: 10,
            filters: BTreeMap::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_top_k(mut self, top_k: usize) -> Self {
        self.top_k = top_k;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct QueryPlan {
    pub original: String,
    pub rewritten: Vec<String>,
    pub subqueries: Vec<String>,
    pub filters: Option<Value>,
    pub rationale_summary: Option<String>,
}

impl QueryPlan {
    pub fn new(original: impl Into<String>) -> Self {
        Self {
            original: original.into(),
            rewritten: Vec::new(),
            subqueries: Vec::new(),
            filters: None,
            rationale_summary: None,
        }
    }

    pub fn with_rewritten<I, S>(mut self, rewritten: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.rewritten = rewritten.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_subqueries<I, S>(mut self, subqueries: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.subqueries = subqueries.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_filter(mut self, filters: Value) -> Self {
        self.filters = Some(filters);
        self
    }

    pub fn with_rationale_summary(mut self, rationale_summary: impl Into<String>) -> Self {
        self.rationale_summary = Some(rationale_summary.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AuthContext {
    pub tenant_id: String,
    pub principal_id: String,
    pub groups: BTreeSet<String>,
    pub roles: BTreeSet<String>,
    pub attributes: BTreeMap<String, Value>,
}

impl AuthContext {
    pub fn new(tenant_id: impl Into<String>, principal_id: impl Into<String>) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            principal_id: principal_id.into(),
            groups: BTreeSet::new(),
            roles: BTreeSet::new(),
            attributes: BTreeMap::new(),
        }
    }

    pub fn with_groups<I, S>(mut self, groups: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.groups = groups.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_roles<I, S>(mut self, roles: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.roles = roles.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_attribute(mut self, name: impl Into<String>, value: Value) -> Self {
        self.attributes.insert(name.into(), value);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RetrievalResult {
    pub retrieval_id: String,
    pub request: SearchRequest,
    pub hits: Vec<SearchHit>,
    pub total_candidates: Option<usize>,
    pub latency_ms: Option<f64>,
    pub warnings: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl RetrievalResult {
    pub fn new(
        retrieval_id: impl Into<String>,
        request: SearchRequest,
        hits: Vec<SearchHit>,
    ) -> Self {
        Self {
            retrieval_id: retrieval_id.into(),
            request,
            hits,
            total_candidates: None,
            latency_ms: None,
            warnings: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FederatedFailureMode {
    Fail,
    Partial,
}

#[derive(Clone, Debug, PartialEq)]
pub struct FederatedRetrievalOptions {
    pub failure_mode: FederatedFailureMode,
    pub fusion_strategy: FusionStrategy,
    pub k: usize,
}

impl FederatedRetrievalOptions {
    pub fn new() -> Self {
        Self {
            failure_mode: FederatedFailureMode::Partial,
            fusion_strategy: FusionStrategy::ReciprocalRankFusion,
            k: 60,
        }
    }

    pub fn with_failure_mode(mut self, failure_mode: FederatedFailureMode) -> Self {
        self.failure_mode = failure_mode;
        self
    }

    pub fn with_fusion_strategy(mut self, fusion_strategy: FusionStrategy) -> Self {
        self.fusion_strategy = fusion_strategy;
        self
    }

    pub fn with_k(mut self, k: usize) -> Self {
        self.k = k;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct FederatedRetrievalSource {
    pub source_id: String,
    pub result: Option<RetrievalResult>,
    pub error: Option<String>,
    pub weight: f64,
    pub metadata: BTreeMap<String, Value>,
}

impl FederatedRetrievalSource {
    pub fn success(source_id: impl Into<String>, result: RetrievalResult) -> Self {
        Self {
            source_id: source_id.into(),
            result: Some(result),
            error: None,
            weight: 1.0,
            metadata: BTreeMap::new(),
        }
    }

    pub fn failure(source_id: impl Into<String>, error: impl Into<String>) -> Self {
        Self {
            source_id: source_id.into(),
            result: None,
            error: Some(error.into()),
            weight: 1.0,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_weight(mut self, weight: f64) -> Self {
        self.weight = weight;
        self
    }

    pub fn with_metadata(mut self, metadata: BTreeMap<String, Value>) -> Self {
        self.metadata = metadata;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct KnowledgeItemRef {
    pub item_id: String,
    pub item_kind: String,
    pub source: SourceRef,
    pub schema_ref: Option<String>,
    pub payload_ref: Option<String>,
    pub preview: Vec<String>,
    pub acl: Option<Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl KnowledgeItemRef {
    pub fn new(
        item_id: impl Into<String>,
        item_kind: impl Into<String>,
        source: SourceRef,
    ) -> Self {
        Self {
            item_id: item_id.into(),
            item_kind: item_kind.into(),
            source,
            schema_ref: None,
            payload_ref: None,
            preview: Vec::new(),
            acl: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_preview<I, S>(mut self, preview: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.preview = preview.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_metadata(mut self, metadata: BTreeMap<String, Value>) -> Self {
        self.metadata = metadata;
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SearchHit {
    pub hit_id: String,
    pub item: KnowledgeItemRef,
    pub rank: usize,
    pub retriever: String,
    pub raw_score: Option<f64>,
    pub normalized_score: Option<f64>,
    pub score_kind: Option<String>,
    pub highlights: Vec<SourceRef>,
    pub metadata: BTreeMap<String, Value>,
}

impl SearchHit {
    pub fn new(
        hit_id: impl Into<String>,
        item: KnowledgeItemRef,
        rank: usize,
        retriever: impl Into<String>,
    ) -> Self {
        Self {
            hit_id: hit_id.into(),
            item,
            rank,
            retriever: retriever.into(),
            raw_score: None,
            normalized_score: None,
            score_kind: None,
            highlights: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_raw_score(mut self, raw_score: f64) -> Self {
        self.raw_score = Some(raw_score);
        self
    }

    pub fn with_normalized_score(mut self, normalized_score: f64) -> Self {
        self.normalized_score = Some(normalized_score);
        self
    }

    pub fn with_score_kind(mut self, score_kind: impl Into<String>) -> Self {
        self.score_kind = Some(score_kind.into());
        self
    }

    pub fn with_highlights<I>(mut self, highlights: I) -> Self
    where
        I: IntoIterator<Item = SourceRef>,
    {
        self.highlights = highlights.into_iter().collect();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ContextPack {
    pub context_id: String,
    pub hits: Vec<SearchHit>,
    pub token_budget: Option<usize>,
    pub token_count: Option<usize>,
    pub metadata: BTreeMap<String, Value>,
}

impl ContextPack {
    pub fn new(context_id: impl Into<String>, hits: Vec<SearchHit>) -> Self {
        Self {
            context_id: context_id.into(),
            hits,
            token_budget: None,
            token_count: None,
            metadata: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextBuildOptions {
    pub token_budget: usize,
    pub reserve_output_tokens: usize,
    pub per_document_max_chunks: Option<usize>,
    pub per_section_max_chunks: Option<usize>,
    pub per_source_max_chunks: Option<usize>,
    pub deduplicate: bool,
    pub minimum_source_modified_at: Option<String>,
}

impl ContextBuildOptions {
    pub fn new(token_budget: usize) -> Self {
        Self {
            token_budget,
            reserve_output_tokens: 0,
            per_document_max_chunks: None,
            per_section_max_chunks: None,
            per_source_max_chunks: None,
            deduplicate: true,
            minimum_source_modified_at: None,
        }
    }

    pub fn with_per_document_max_chunks(mut self, per_document_max_chunks: usize) -> Self {
        self.per_document_max_chunks = Some(per_document_max_chunks);
        self
    }

    pub fn with_reserve_output_tokens(mut self, reserve_output_tokens: usize) -> Self {
        self.reserve_output_tokens = reserve_output_tokens;
        self
    }

    pub fn with_per_section_max_chunks(mut self, per_section_max_chunks: usize) -> Self {
        self.per_section_max_chunks = Some(per_section_max_chunks);
        self
    }

    pub fn with_per_source_max_chunks(mut self, per_source_max_chunks: usize) -> Self {
        self.per_source_max_chunks = Some(per_source_max_chunks);
        self
    }

    pub fn with_minimum_source_modified_at(
        mut self,
        minimum_source_modified_at: impl Into<String>,
    ) -> Self {
        self.minimum_source_modified_at = Some(minimum_source_modified_at.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FusionStrategy {
    Concatenate,
    ReciprocalRankFusion,
    WeightedRank,
    NormalizedScore,
    Interleave,
}

#[derive(Clone, Debug, PartialEq)]
pub struct FusionOptions {
    pub strategy: FusionStrategy,
    pub k: usize,
    pub weights: Option<Vec<f64>>,
    pub retriever_id: String,
}

impl FusionOptions {
    pub fn new() -> Self {
        Self {
            strategy: FusionStrategy::ReciprocalRankFusion,
            k: 60,
            weights: None,
            retriever_id: "fused".to_owned(),
        }
    }

    pub fn with_strategy(mut self, strategy: FusionStrategy) -> Self {
        self.strategy = strategy;
        self
    }

    pub fn with_k(mut self, k: usize) -> Self {
        self.k = k;
        self
    }

    pub fn with_weights<I>(mut self, weights: I) -> Self
    where
        I: IntoIterator<Item = f64>,
    {
        self.weights = Some(weights.into_iter().collect());
        self
    }

    pub fn with_retriever_id(mut self, retriever_id: impl Into<String>) -> Self {
        self.retriever_id = retriever_id.into();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RerankOptions {
    pub reranker_id: String,
    pub query_terms: Vec<String>,
    pub input_limit: Option<usize>,
}

impl RerankOptions {
    pub fn new(reranker_id: impl Into<String>) -> Self {
        Self {
            reranker_id: reranker_id.into(),
            query_terms: Vec::new(),
            input_limit: None,
        }
    }

    pub fn with_query_terms<I, S>(mut self, query_terms: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.query_terms = query_terms
            .into_iter()
            .map(|term| term.into().to_ascii_lowercase())
            .filter(|term| !term.is_empty())
            .collect();
        self
    }

    pub fn with_input_limit(mut self, input_limit: usize) -> Self {
        self.input_limit = Some(input_limit);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RankedHit {
    pub hit: SearchHit,
    pub rerank_score: Option<f64>,
    pub reranker: Option<String>,
    pub explanation: Option<String>,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RerankResult {
    pub ranked_hits: Vec<RankedHit>,
    pub reranker: String,
    pub input_count: usize,
    pub evaluated_count: usize,
    pub truncated_hit_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KnowledgeDeleteMode {
    Tombstone,
    Hard,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KnowledgeRecordStatus {
    Active,
    Tombstoned,
}

#[derive(Clone, Debug, PartialEq)]
pub struct KnowledgeIndexRecord {
    pub chunk: DocumentChunk,
    pub status: KnowledgeRecordStatus,
}

#[derive(Clone, Debug, PartialEq)]
pub struct KnowledgeWriteReport {
    pub operation: String,
    pub affected_count: usize,
    pub chunk_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct KnowledgePublishResult {
    pub index_id: String,
    pub asset_id: String,
    pub revision_id: String,
    pub published_chunk_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KnowledgeIndexCapabilities {
    pub upsert: bool,
    pub delete: bool,
    pub metadata_update: bool,
    pub acl_update: bool,
    pub publish: bool,
    pub hard_delete: bool,
    pub tombstone: bool,
    pub retriever_adapter: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KnowledgeIndexHealth {
    pub healthy: bool,
    pub indexed_chunks: usize,
    pub active_chunks: usize,
    pub tombstoned_chunks: usize,
    pub published_revisions: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub struct InMemoryKnowledgeIndex {
    pub index_id: String,
    records: BTreeMap<String, KnowledgeIndexRecord>,
    published_revisions: BTreeMap<String, String>,
}

impl InMemoryKnowledgeIndex {
    pub fn new(index_id: impl Into<String>) -> Self {
        Self {
            index_id: index_id.into(),
            records: BTreeMap::new(),
            published_revisions: BTreeMap::new(),
        }
    }

    pub fn upsert_chunks<I>(&mut self, chunks: I) -> KnowledgeWriteReport
    where
        I: IntoIterator<Item = DocumentChunk>,
    {
        let mut chunk_ids = Vec::new();
        for chunk in chunks {
            let chunk_id = chunk.chunk_id.clone();
            chunk_ids.push(chunk_id.clone());
            self.records.insert(
                chunk_id,
                KnowledgeIndexRecord {
                    chunk,
                    status: KnowledgeRecordStatus::Active,
                },
            );
        }
        let mut metadata = BTreeMap::new();
        metadata.insert("index_id".to_owned(), json!(self.index_id.clone()));
        KnowledgeWriteReport {
            operation: "upsert".to_owned(),
            affected_count: chunk_ids.len(),
            chunk_ids,
            metadata,
        }
    }

    pub fn delete_asset(
        &mut self,
        asset_id: &str,
        mode: KnowledgeDeleteMode,
    ) -> Result<KnowledgeWriteReport, RagError> {
        let chunk_ids = self
            .records
            .iter()
            .filter(|(_chunk_id, record)| record.chunk.asset_id == asset_id)
            .map(|(chunk_id, _record)| chunk_id.clone())
            .collect::<Vec<_>>();
        match mode {
            KnowledgeDeleteMode::Hard => {
                for chunk_id in &chunk_ids {
                    self.records.remove(chunk_id);
                }
            }
            KnowledgeDeleteMode::Tombstone => {
                for chunk_id in &chunk_ids {
                    if let Some(record) = self.records.get_mut(chunk_id) {
                        record.status = KnowledgeRecordStatus::Tombstoned;
                    }
                }
            }
        }
        self.published_revisions.remove(asset_id);
        let mut metadata = BTreeMap::new();
        metadata.insert("asset_id".to_owned(), json!(asset_id));
        metadata.insert(
            "delete_mode".to_owned(),
            json!(match mode {
                KnowledgeDeleteMode::Hard => "hard",
                KnowledgeDeleteMode::Tombstone => "tombstone",
            }),
        );
        Ok(KnowledgeWriteReport {
            operation: "delete".to_owned(),
            affected_count: chunk_ids.len(),
            chunk_ids,
            metadata,
        })
    }

    pub fn update_chunk_metadata(
        &mut self,
        chunk_id: &str,
        metadata: BTreeMap<String, Value>,
    ) -> Result<KnowledgeWriteReport, RagError> {
        let Some(record) = self.records.get_mut(chunk_id) else {
            return Err(RagError::KnowledgeItemNotFound {
                item_id: chunk_id.to_owned(),
            });
        };
        let metadata_keys = metadata.keys().cloned().collect::<Vec<_>>();
        for (key, value) in metadata {
            record.chunk.metadata.insert(key, value);
        }
        let mut report_metadata = BTreeMap::new();
        report_metadata.insert("metadata_keys".to_owned(), json!(metadata_keys));
        Ok(KnowledgeWriteReport {
            operation: "update_metadata".to_owned(),
            affected_count: 1,
            chunk_ids: vec![chunk_id.to_owned()],
            metadata: report_metadata,
        })
    }

    pub fn update_chunk_acl(
        &mut self,
        chunk_id: &str,
        acl: Option<Value>,
    ) -> Result<KnowledgeWriteReport, RagError> {
        let Some(record) = self.records.get_mut(chunk_id) else {
            return Err(RagError::KnowledgeItemNotFound {
                item_id: chunk_id.to_owned(),
            });
        };
        record.chunk.acl = acl;
        Ok(KnowledgeWriteReport {
            operation: "update_acl".to_owned(),
            affected_count: 1,
            chunk_ids: vec![chunk_id.to_owned()],
            metadata: BTreeMap::new(),
        })
    }

    pub fn publish_revision(
        &mut self,
        asset_id: &str,
        revision_id: &str,
    ) -> Result<KnowledgePublishResult, RagError> {
        let published_chunk_ids = self
            .records
            .iter()
            .filter(|(_chunk_id, record)| {
                record.status == KnowledgeRecordStatus::Active
                    && record.chunk.asset_id == asset_id
                    && record.chunk.revision_id == revision_id
            })
            .map(|(chunk_id, _record)| chunk_id.clone())
            .collect::<Vec<_>>();
        if published_chunk_ids.is_empty() {
            return Err(RagError::KnowledgeItemNotFound {
                item_id: format!("{asset_id}:{revision_id}"),
            });
        }
        self.published_revisions
            .insert(asset_id.to_owned(), revision_id.to_owned());
        let mut metadata = BTreeMap::new();
        metadata.insert(
            "active_chunk_count".to_owned(),
            json!(published_chunk_ids.len()),
        );
        Ok(KnowledgePublishResult {
            index_id: self.index_id.clone(),
            asset_id: asset_id.to_owned(),
            revision_id: revision_id.to_owned(),
            published_chunk_ids,
            metadata,
        })
    }

    pub fn is_revision_published(&self, asset_id: &str, revision_id: &str) -> bool {
        self.published_revisions
            .get(asset_id)
            .is_some_and(|published| published == revision_id)
    }

    pub fn capabilities(&self) -> KnowledgeIndexCapabilities {
        KnowledgeIndexCapabilities {
            upsert: true,
            delete: true,
            metadata_update: true,
            acl_update: true,
            publish: true,
            hard_delete: true,
            tombstone: true,
            retriever_adapter: true,
        }
    }

    pub fn health(&self) -> KnowledgeIndexHealth {
        let tombstoned_chunks = self
            .records
            .values()
            .filter(|record| record.status == KnowledgeRecordStatus::Tombstoned)
            .count();
        KnowledgeIndexHealth {
            healthy: true,
            indexed_chunks: self.records.len(),
            active_chunks: self.records.len() - tombstoned_chunks,
            tombstoned_chunks,
            published_revisions: self.published_revisions.len(),
        }
    }

    pub fn record(&self, chunk_id: &str) -> Option<&KnowledgeIndexRecord> {
        self.records.get(chunk_id)
    }

    pub fn retriever(&self, retriever_id: impl Into<String>) -> InMemoryChunkRetriever {
        let mut chunks = self
            .records
            .values()
            .filter(|record| record.status == KnowledgeRecordStatus::Active)
            .map(|record| record.chunk.clone())
            .collect::<Vec<_>>();
        chunks.sort_by(|left, right| {
            left.asset_id
                .cmp(&right.asset_id)
                .then_with(|| left.revision_id.cmp(&right.revision_id))
                .then_with(|| left.chunk_id.cmp(&right.chunk_id))
        });
        InMemoryChunkRetriever::new(chunks, retriever_id)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FailurePolicy {
    Warn,
    Fail,
    Abstain,
    Repair,
    RemoveInvalid,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Citation {
    pub citation_id: String,
    pub source: SourceRef,
    pub claim_id: Option<String>,
    pub cited_text: Option<String>,
    pub confidence: Option<f64>,
    pub metadata: BTreeMap<String, Value>,
}

impl Citation {
    pub fn new(citation_id: impl Into<String>, source: SourceRef) -> Self {
        Self {
            citation_id: citation_id.into(),
            source,
            claim_id: None,
            cited_text: None,
            confidence: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_cited_text(mut self, cited_text: impl Into<String>) -> Self {
        self.cited_text = Some(cited_text.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Claim {
    pub claim_id: String,
    pub text: String,
    pub citation_ids: Vec<String>,
    pub metadata: BTreeMap<String, Value>,
}

impl Claim {
    pub fn new(claim_id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            claim_id: claim_id.into(),
            text: text.into(),
            citation_ids: Vec::new(),
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_citation_ids<I, S>(mut self, citation_ids: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.citation_ids = citation_ids.into_iter().map(Into::into).collect();
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Abstention {
    pub reason: String,
    pub user_message: String,
    pub diagnostics: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Answer {
    pub answer_id: String,
    pub text: String,
    pub claims: Vec<Claim>,
    pub citations: Vec<Citation>,
    pub abstention: Option<Abstention>,
    pub metadata: BTreeMap<String, Value>,
}

impl Answer {
    pub fn new(answer_id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            answer_id: answer_id.into(),
            text: text.into(),
            claims: Vec::new(),
            citations: Vec::new(),
            abstention: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_claim(mut self, claim: Claim) -> Self {
        self.claims.push(claim);
        self
    }

    pub fn with_citation(mut self, citation: Citation) -> Self {
        self.citations.push(citation);
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RagResultPayload {
    pub query_plan: QueryPlan,
    pub retrievals: Vec<RetrievalResult>,
    pub context: ContextPack,
    pub model_response: Value,
    pub answer: Answer,
}

impl RagResultPayload {
    pub fn new(
        query_plan: QueryPlan,
        retrievals: Vec<RetrievalResult>,
        context: ContextPack,
        model_response: Value,
        answer: Answer,
    ) -> Self {
        Self {
            query_plan,
            retrievals,
            context,
            model_response,
            answer,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RagResultBundle {
    pub base: ResultBundle,
    pub profile: String,
    pub payload: RagResultPayload,
}

impl RagResultBundle {
    pub fn new(base: ResultBundle, payload: RagResultPayload) -> Self {
        Self {
            base,
            profile: "rag".to_owned(),
            payload,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CitationSeverity {
    Warning,
    Error,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CitationValidationIssue {
    pub code: String,
    pub message: String,
    pub citation_id: Option<String>,
    pub claim_id: Option<String>,
    pub severity: CitationSeverity,
    pub metadata: BTreeMap<String, Value>,
}

impl CitationValidationIssue {
    pub fn new(
        code: impl Into<String>,
        message: impl Into<String>,
        severity: CitationSeverity,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            citation_id: None,
            claim_id: None,
            severity,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_citation_id(mut self, citation_id: impl Into<String>) -> Self {
        self.citation_id = Some(citation_id.into());
        self
    }

    pub fn with_claim_id(mut self, claim_id: impl Into<String>) -> Self {
        self.claim_id = Some(claim_id.into());
        self
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CitationValidationResult {
    pub ok: bool,
    pub issues: Vec<CitationValidationIssue>,
    pub abstention: Option<Abstention>,
    pub repaired_answer: Option<Answer>,
}

impl CitationValidationResult {
    pub fn ok() -> Self {
        Self {
            ok: true,
            issues: Vec::new(),
            abstention: None,
            repaired_answer: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CitationSourceTrace {
    pub citation_id: String,
    pub claim_id: Option<String>,
    pub context_id: String,
    pub hit_id: String,
    pub retriever: String,
    pub item_id: String,
    pub item_kind: String,
    pub source: SourceRef,
    pub locator: Option<DocumentSpan>,
    pub acl: Option<Value>,
    pub element_ids: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RagError {
    InvalidPerDocumentMaxChunks,
    InvalidPerSectionMaxChunks,
    InvalidPerSourceMaxChunks,
    InvalidFusionK,
    WeightCountMismatch,
    InvalidRerankInputLimit,
    InvalidModelResponse {
        message: String,
    },
    FederatedSourceFailed {
        source_id: String,
        message: String,
    },
    KnowledgeItemNotFound {
        item_id: String,
    },
    AuthContextRequired {
        resource_id: String,
    },
    InvalidAcl {
        resource_id: String,
        message: String,
    },
    CitationNotFound {
        citation_id: String,
    },
    CitationSourceNotInContext {
        citation_id: String,
    },
}

impl fmt::Display for RagError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPerDocumentMaxChunks => {
                write!(formatter, "per_document_max_chunks must be at least 1")
            }
            Self::InvalidPerSectionMaxChunks => {
                write!(formatter, "per_section_max_chunks must be at least 1")
            }
            Self::InvalidPerSourceMaxChunks => {
                write!(formatter, "per_source_max_chunks must be at least 1")
            }
            Self::InvalidFusionK => write!(formatter, "fusion k must be at least 1"),
            Self::WeightCountMismatch => {
                write!(formatter, "weights length must match hit_sets length")
            }
            Self::InvalidRerankInputLimit => {
                write!(formatter, "rerank input limit must be at least 1")
            }
            Self::InvalidModelResponse { message } => write!(formatter, "{message}"),
            Self::FederatedSourceFailed { source_id, message } => {
                write!(formatter, "federated source {source_id} failed: {message}")
            }
            Self::KnowledgeItemNotFound { item_id } => {
                write!(formatter, "knowledge item {item_id:?} was not found")
            }
            Self::AuthContextRequired { resource_id } => {
                write!(
                    formatter,
                    "auth context is required to access {resource_id:?}"
                )
            }
            Self::InvalidAcl {
                resource_id,
                message,
            } => write!(formatter, "ACL for {resource_id:?} is invalid: {message}"),
            Self::CitationNotFound { citation_id } => {
                write!(formatter, "citation {citation_id:?} was not found")
            }
            Self::CitationSourceNotInContext { citation_id } => {
                write!(
                    formatter,
                    "citation {citation_id:?} does not point to the current context"
                )
            }
        }
    }
}

impl Error for RagError {}

pub fn knowledge_item_from_chunk(chunk: &DocumentChunk) -> KnowledgeItemRef {
    let source = chunk.source_refs.first().cloned().unwrap_or_else(|| {
        SourceRef::document_chunk(
            &chunk.chunk_id,
            &chunk.revision_id,
            "",
            DocumentSpan::new(&chunk.asset_id, &chunk.revision_id, &chunk.document_id)
                .with_chunk_id(&chunk.chunk_id),
        )
    });
    let mut metadata = chunk.metadata.clone();
    metadata.insert("document_id".to_owned(), json!(chunk.document_id));
    metadata.insert("asset_id".to_owned(), json!(chunk.asset_id));
    metadata.insert("revision_id".to_owned(), json!(chunk.revision_id));
    metadata.insert("element_ids".to_owned(), json!(chunk.element_ids));
    let mut item = KnowledgeItemRef::new(&chunk.chunk_id, "document_chunk", source)
        .with_preview([chunk.text.as_str()])
        .with_metadata(metadata);
    item.acl = chunk.acl.clone();
    item
}

pub fn authorize_search_hits(
    hits: &[SearchHit],
    auth: Option<&AuthContext>,
) -> Result<Vec<SearchHit>, RagError> {
    let mut authorized = Vec::new();
    for hit in hits {
        if acl_allows(&hit.hit_id, &hit.item.acl, auth)? {
            authorized.push(hit.clone());
        }
    }
    Ok(authorized)
}

#[derive(Clone, Debug, PartialEq)]
pub struct InMemoryChunkRetriever {
    pub chunks: Vec<DocumentChunk>,
    pub retriever_id: String,
}

impl InMemoryChunkRetriever {
    pub fn new<I>(chunks: I, retriever_id: impl Into<String>) -> Self
    where
        I: IntoIterator<Item = DocumentChunk>,
    {
        Self {
            chunks: chunks.into_iter().collect(),
            retriever_id: retriever_id.into(),
        }
    }

    pub fn search(&self, query_text: impl Into<String>, top_k: usize) -> Vec<SearchHit> {
        self.retrieve(SearchRequest::new(query_text).with_top_k(top_k))
            .hits
    }

    pub fn retrieve(&self, request: SearchRequest) -> RetrievalResult {
        let request_hash = canonical_hash(&json!({
            "query_text": &request.query_text,
            "top_k": request.top_k,
            "filters": &request.filters,
        }));
        let retrieval_id = format!("{}:{request_hash}", self.retriever_id);
        let mut terms = Vec::new();
        let mut current = String::new();
        for character in request.query_text.chars() {
            if character.is_ascii_alphanumeric() || character == '_' {
                current.push(character.to_ascii_lowercase());
            } else if !current.is_empty() {
                terms.push(std::mem::take(&mut current));
            }
        }
        if !current.is_empty() {
            terms.push(current);
        }
        if terms.is_empty() {
            let mut result = RetrievalResult::new(retrieval_id, request, Vec::new());
            result.total_candidates = Some(0);
            return result;
        }
        let mut scored = Vec::new();
        for (index, chunk) in self.chunks.iter().enumerate() {
            let haystack = chunk.text.to_ascii_lowercase();
            let score = terms
                .iter()
                .map(|term| haystack.matches(term).count())
                .sum::<usize>();
            if score > 0 {
                scored.push((score, index, chunk));
            }
        }
        scored.sort_by(|left, right| right.0.cmp(&left.0).then_with(|| left.1.cmp(&right.1)));
        if scored.is_empty() {
            let mut result = RetrievalResult::new(retrieval_id, request, Vec::new());
            result.total_candidates = Some(0);
            return result;
        }
        let max_score = scored[0].0 as f64;
        let mut hits = Vec::new();
        for (rank, (score, _index, chunk)) in scored.iter().take(request.top_k).enumerate() {
            hits.push(
                SearchHit::new(
                    format!("{}:{}", self.retriever_id, chunk.chunk_id),
                    knowledge_item_from_chunk(chunk),
                    rank + 1,
                    &self.retriever_id,
                )
                .with_raw_score(*score as f64)
                .with_normalized_score(*score as f64 / max_score)
                .with_score_kind("term_frequency")
                .with_highlights(chunk.source_refs.clone()),
            );
        }
        let mut result = RetrievalResult::new(retrieval_id, request, hits);
        result.total_candidates = Some(scored.len());
        result
    }
}

pub fn build_context_pack(
    context_id: impl Into<String>,
    mut hits: Vec<SearchHit>,
    options: ContextBuildOptions,
) -> Result<ContextPack, RagError> {
    if matches!(options.per_document_max_chunks, Some(0)) {
        return Err(RagError::InvalidPerDocumentMaxChunks);
    }
    if matches!(options.per_section_max_chunks, Some(0)) {
        return Err(RagError::InvalidPerSectionMaxChunks);
    }
    if matches!(options.per_source_max_chunks, Some(0)) {
        return Err(RagError::InvalidPerSourceMaxChunks);
    }
    hits.sort_by(|left, right| {
        left.rank
            .cmp(&right.rank)
            .then_with(|| left.hit_id.cmp(&right.hit_id))
    });
    let effective_context_token_budget = options
        .token_budget
        .saturating_sub(options.reserve_output_tokens);
    let mut selected = Vec::new();
    let mut selected_hit_ids = Vec::new();
    let mut dropped_hit_ids = Vec::new();
    let mut drop_reasons = Map::new();
    let mut selected_item_ids = BTreeSet::new();
    let mut chunks_per_document: BTreeMap<String, usize> = BTreeMap::new();
    let mut chunks_per_section: BTreeMap<String, usize> = BTreeMap::new();
    let mut chunks_per_source: BTreeMap<String, usize> = BTreeMap::new();
    let mut token_count = 0;

    for hit in hits {
        if options.deduplicate && selected_item_ids.contains(&hit.item.item_id) {
            dropped_hit_ids.push(hit.hit_id.clone());
            drop_reasons.insert(hit.hit_id.clone(), json!("duplicate"));
            continue;
        }
        let document_id = hit
            .item
            .metadata
            .get("document_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| {
                hit.item
                    .source
                    .locator
                    .as_ref()
                    .map(|locator| locator.document_id.clone())
            })
            .unwrap_or_else(|| hit.item.item_id.clone());
        let current_document_chunks = chunks_per_document.get(&document_id).copied().unwrap_or(0);
        if options
            .per_document_max_chunks
            .is_some_and(|limit| current_document_chunks >= limit)
        {
            dropped_hit_ids.push(hit.hit_id.clone());
            drop_reasons.insert(hit.hit_id.clone(), json!("per_document_max_chunks"));
            continue;
        }
        let section_id = hit
            .metadata
            .get("section_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| {
                hit.item
                    .metadata
                    .get("section_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .or_else(|| {
                std::iter::once(&hit.item.source)
                    .chain(hit.highlights.iter())
                    .find_map(|source| {
                        source
                            .locator
                            .as_ref()
                            .and_then(|locator| locator.element_id.clone())
                    })
            });
        let current_section_chunks = section_id
            .as_ref()
            .map(|section_id| chunks_per_section.get(section_id).copied().unwrap_or(0))
            .unwrap_or(0);
        if section_id.is_some()
            && options
                .per_section_max_chunks
                .is_some_and(|limit| current_section_chunks >= limit)
        {
            dropped_hit_ids.push(hit.hit_id.clone());
            drop_reasons.insert(hit.hit_id.clone(), json!("per_section_max_chunks"));
            continue;
        }
        let source_id = hit
            .metadata
            .get("source_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| hit.retriever.clone());
        let current_source_chunks = chunks_per_source.get(&source_id).copied().unwrap_or(0);
        if options
            .per_source_max_chunks
            .is_some_and(|limit| current_source_chunks >= limit)
        {
            dropped_hit_ids.push(hit.hit_id.clone());
            drop_reasons.insert(hit.hit_id.clone(), json!("per_source_max_chunks"));
            continue;
        }
        if let Some(minimum_source_modified_at) = &options.minimum_source_modified_at {
            let source_modified_at = hit
                .metadata
                .get("source_modified_at")
                .and_then(Value::as_str)
                .or_else(|| {
                    hit.item
                        .metadata
                        .get("source_modified_at")
                        .and_then(Value::as_str)
                });
            if source_modified_at.is_none_or(|value| value < minimum_source_modified_at.as_str()) {
                dropped_hit_ids.push(hit.hit_id.clone());
                drop_reasons.insert(hit.hit_id.clone(), json!("freshness"));
                continue;
            }
        }
        let estimated_tokens = hit
            .item
            .preview
            .iter()
            .map(|preview| preview.split_whitespace().count())
            .sum::<usize>();
        if token_count + estimated_tokens > effective_context_token_budget {
            dropped_hit_ids.push(hit.hit_id.clone());
            drop_reasons.insert(hit.hit_id.clone(), json!("token_budget"));
            continue;
        }
        selected_hit_ids.push(hit.hit_id.clone());
        selected_item_ids.insert(hit.item.item_id.clone());
        chunks_per_document.insert(document_id, current_document_chunks + 1);
        if let Some(section_id) = section_id {
            chunks_per_section.insert(section_id, current_section_chunks + 1);
        }
        chunks_per_source.insert(source_id, current_source_chunks + 1);
        token_count += estimated_tokens;
        selected.push(hit);
    }

    let mut context = ContextPack::new(context_id, selected);
    context.token_budget = Some(options.token_budget);
    context.token_count = Some(token_count);
    context
        .metadata
        .insert("selected_hit_ids".to_owned(), json!(selected_hit_ids));
    context
        .metadata
        .insert("dropped_hit_ids".to_owned(), json!(dropped_hit_ids));
    context
        .metadata
        .insert("drop_reasons".to_owned(), Value::Object(drop_reasons));
    if let Some(minimum_source_modified_at) = options.minimum_source_modified_at {
        context.metadata.insert(
            "minimum_source_modified_at".to_owned(),
            json!(minimum_source_modified_at),
        );
    }
    if let Some(per_section_max_chunks) = options.per_section_max_chunks {
        context.metadata.insert(
            "per_section_max_chunks".to_owned(),
            json!(per_section_max_chunks),
        );
    }
    if let Some(per_source_max_chunks) = options.per_source_max_chunks {
        context.metadata.insert(
            "per_source_max_chunks".to_owned(),
            json!(per_source_max_chunks),
        );
    }
    if options.reserve_output_tokens > 0 {
        context.metadata.insert(
            "reserve_output_tokens".to_owned(),
            json!(options.reserve_output_tokens),
        );
        context.metadata.insert(
            "effective_context_token_budget".to_owned(),
            json!(effective_context_token_budget),
        );
    }
    Ok(context)
}

pub fn render_context_pack(context: &ContextPack) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "GRAPHBLOCKS_CONTEXT_PACK_BEGIN {}",
        serde_json::to_string(&json!({
            "context_id": &context.context_id,
            "trust_boundary": "retrieved_untrusted",
        }))
        .expect("context metadata should serialize")
    ));
    for hit in &context.hits {
        let source_refs = if hit.highlights.is_empty() {
            vec![&hit.item.source]
        } else {
            hit.highlights.iter().collect::<Vec<_>>()
        };
        let sources = source_refs
            .iter()
            .map(|source| {
                json!({
                    "source_id": &source.source_id,
                    "source_kind": &source.source_kind,
                    "revision": &source.revision,
                    "digest": &source.digest,
                    "locator": source.locator.as_ref().map(|locator| {
                        json!({
                            "asset_id": &locator.asset_id,
                            "revision_id": &locator.revision_id,
                            "document_id": &locator.document_id,
                            "element_id": &locator.element_id,
                            "chunk_id": &locator.chunk_id,
                            "page": locator.page,
                            "bbox": &locator.bbox,
                            "char_start": locator.char_start,
                            "char_end": locator.char_end,
                            "sheet": &locator.sheet,
                            "cell_range": &locator.cell_range,
                            "slide": locator.slide,
                        })
                    }),
                    "observed_at": &source.observed_at,
                    "relevant_as_of": &source.relevant_as_of,
                    "trust": "retrieved_untrusted",
                })
            })
            .collect::<Vec<_>>();
        lines.push(format!(
            "GRAPHBLOCKS_RETRIEVED_ITEM_BEGIN {}",
            serde_json::to_string(&json!({
                "hit_id": &hit.hit_id,
                "item_id": &hit.item.item_id,
                "rank": hit.rank,
                "retriever": &hit.retriever,
                "sources": sources,
                "trust": "retrieved_untrusted",
            }))
            .expect("item metadata should serialize")
        ));
        lines.push(
            serde_json::to_string(&hit.item.preview.join("\n"))
                .expect("context content should serialize"),
        );
        lines.push("GRAPHBLOCKS_RETRIEVED_ITEM_END".to_owned());
    }
    lines.push("GRAPHBLOCKS_CONTEXT_PACK_END".to_owned());
    lines.join("\n")
}

pub fn build_answer_from_model_response(
    answer_id: impl Into<String>,
    model_response: &Value,
) -> Result<Answer, RagError> {
    build_answer_from_model_response_inner(answer_id.into(), model_response, None)
}

pub fn build_answer_from_model_response_with_context(
    answer_id: impl Into<String>,
    model_response: &Value,
    context: &ContextPack,
) -> Result<Answer, RagError> {
    build_answer_from_model_response_inner(answer_id.into(), model_response, Some(context))
}

fn build_answer_from_model_response_inner(
    answer_id: String,
    model_response: &Value,
    context: Option<&ContextPack>,
) -> Result<Answer, RagError> {
    let text = model_response
        .get("output_text")
        .and_then(Value::as_str)
        .or_else(|| model_response.get("text").and_then(Value::as_str))
        .ok_or_else(|| RagError::InvalidModelResponse {
            message: "model_response must contain string output_text or text".to_owned(),
        })?;
    let mut answer = Answer::new(answer_id, text);
    if let Some(claims) = model_response.get("claims").and_then(Value::as_array) {
        for claim_value in claims {
            let claim_id = claim_value
                .get("claim_id")
                .and_then(Value::as_str)
                .ok_or_else(|| RagError::InvalidModelResponse {
                    message: "model_response claims must contain string claim_id and text"
                        .to_owned(),
                })?;
            let claim_text = claim_value
                .get("text")
                .and_then(Value::as_str)
                .ok_or_else(|| RagError::InvalidModelResponse {
                    message: "model_response claims must contain string claim_id and text"
                        .to_owned(),
                })?;
            let citation_ids = claim_value
                .get("citation_ids")
                .and_then(Value::as_array)
                .map(|values| {
                    values
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            answer
                .claims
                .push(Claim::new(claim_id, claim_text).with_citation_ids(citation_ids));
        }
    }
    if let Some(citations) = model_response.get("citations").and_then(Value::as_array) {
        let context = context.ok_or_else(|| RagError::InvalidModelResponse {
            message: "model_response citations require context for source resolution".to_owned(),
        })?;
        for citation_value in citations {
            let citation_id = citation_value
                .get("citation_id")
                .and_then(Value::as_str)
                .ok_or_else(|| RagError::InvalidModelResponse {
                    message:
                        "model_response citations must contain string citation_id and source_id"
                            .to_owned(),
                })?;
            let source_id = citation_value
                .get("source_id")
                .and_then(Value::as_str)
                .ok_or_else(|| RagError::InvalidModelResponse {
                    message:
                        "model_response citations must contain string citation_id and source_id"
                            .to_owned(),
                })?;
            let source = context
                .hits
                .iter()
                .flat_map(|hit| std::iter::once(&hit.item.source).chain(hit.highlights.iter()))
                .find(|source| source.source_id == source_id)
                .cloned()
                .ok_or_else(|| RagError::InvalidModelResponse {
                    message: format!("citation source '{source_id}' was not found in context"),
                })?;
            let mut citation = Citation::new(citation_id, source);
            if let Some(claim_id) = citation_value.get("claim_id").and_then(Value::as_str) {
                citation.claim_id = Some(claim_id.to_owned());
            }
            if let Some(cited_text) = citation_value.get("cited_text").and_then(Value::as_str) {
                citation.cited_text = Some(cited_text.to_owned());
            }
            citation.confidence = citation_value.get("confidence").and_then(Value::as_f64);
            answer.citations.push(citation);
        }
    }
    answer.metadata.insert(
        "model_response_digest".to_owned(),
        json!(canonical_hash(model_response)),
    );
    if let Some(response_id) = model_response.get("response_id").and_then(Value::as_str) {
        answer
            .metadata
            .insert("provider_response_id".to_owned(), json!(response_id));
    }
    for key in ["provider", "model", "finish_reason"] {
        if let Some(value) = model_response.get(key).and_then(Value::as_str) {
            answer.metadata.insert(key.to_owned(), json!(value));
        }
    }
    Ok(answer)
}

pub fn build_abstention_answer(
    answer_id: impl Into<String>,
    reason: impl Into<String>,
    user_message: impl Into<String>,
    diagnostics: BTreeMap<String, Value>,
) -> Answer {
    let user_message = user_message.into();
    let mut answer = Answer::new(answer_id, user_message.clone());
    answer.abstention = Some(Abstention {
        reason: reason.into(),
        user_message,
        diagnostics,
    });
    answer
        .metadata
        .insert("answer_kind".to_owned(), json!("abstention"));
    answer
}

pub fn federated_retrieve(
    retriever_id: impl Into<String>,
    request: SearchRequest,
    sources: Vec<FederatedRetrievalSource>,
    options: FederatedRetrievalOptions,
) -> Result<RetrievalResult, RagError> {
    let retriever_id = retriever_id.into();
    let mut hit_sets = Vec::new();
    let mut weights = Vec::new();
    let mut successful_sources = Vec::new();
    let mut failed_sources = Vec::new();
    let mut warnings = Vec::new();
    let mut total_candidates = 0usize;

    for source in sources {
        match (source.result, source.error) {
            (Some(result), None) => {
                total_candidates += result.hits.len();
                weights.push(source.weight);
                hit_sets.push(result.hits);
                successful_sources.push(source.source_id);
            }
            (result, error) => {
                let message = error.unwrap_or_else(|| "missing retrieval result".to_owned());
                if options.failure_mode == FederatedFailureMode::Fail || result.is_some() {
                    return Err(RagError::FederatedSourceFailed {
                        source_id: source.source_id,
                        message,
                    });
                }
                warnings.push(format!(
                    "federated source {} failed: {}",
                    source.source_id, message
                ));
                failed_sources.push(json!({
                    "source_id": source.source_id,
                    "error": message,
                }));
            }
        }
    }

    let mut hits = fuse_search_hits(
        &hit_sets,
        FusionOptions::new()
            .with_strategy(options.fusion_strategy.clone())
            .with_k(options.k)
            .with_weights(weights)
            .with_retriever_id(&retriever_id),
    )?;
    hits.truncate(request.top_k);
    let retrieval_id = format!(
        "{}:{}",
        retriever_id,
        canonical_hash(&json!({
            "query_text": &request.query_text,
            "top_k": request.top_k,
            "filters": &request.filters,
            "successful_sources": &successful_sources,
            "failed_sources": &failed_sources,
            "fusion_strategy": fusion_strategy_label(&options.fusion_strategy),
        }))
    );
    let mut result = RetrievalResult::new(retrieval_id, request, hits);
    result.total_candidates = Some(total_candidates);
    result.warnings = warnings;
    result
        .metadata
        .insert("successful_sources".to_owned(), json!(successful_sources));
    result
        .metadata
        .insert("failed_sources".to_owned(), Value::Array(failed_sources));
    result.metadata.insert(
        "fusion_strategy".to_owned(),
        json!(fusion_strategy_label(&options.fusion_strategy)),
    );
    result
        .metadata
        .insert("retriever_id".to_owned(), json!(retriever_id));
    Ok(result)
}

pub fn fuse_search_hits(
    hit_sets: &[Vec<SearchHit>],
    options: FusionOptions,
) -> Result<Vec<SearchHit>, RagError> {
    if options.k < 1 {
        return Err(RagError::InvalidFusionK);
    }
    if options
        .weights
        .as_ref()
        .is_some_and(|weights| weights.len() != hit_sets.len())
    {
        return Err(RagError::WeightCountMismatch);
    }

    let dedupe_key_for = |hit: &SearchHit| -> String {
        let locator = std::iter::once(&hit.item.source)
            .chain(hit.highlights.iter())
            .find_map(|source| source.locator.as_ref());
        locator.map_or_else(
            || format!("item:{}", hit.item.item_id),
            |locator| {
                format!(
                    "source_span:{}",
                    serde_json::to_string(&json!({
                        "asset_id": &locator.asset_id,
                        "revision_id": &locator.revision_id,
                        "document_id": &locator.document_id,
                        "element_id": &locator.element_id,
                        "chunk_id": &locator.chunk_id,
                        "page": locator.page,
                        "bbox": &locator.bbox,
                        "char_start": locator.char_start,
                        "char_end": locator.char_end,
                        "sheet": &locator.sheet,
                        "cell_range": &locator.cell_range,
                        "slide": locator.slide,
                    }))
                    .expect("source span should serialize")
                )
            },
        )
    };

    let mut grouped_hits: BTreeMap<String, Vec<SearchHit>> = BTreeMap::new();
    let mut first_seen_keys = Vec::new();
    let mut fusion_scores: BTreeMap<String, f64> = BTreeMap::new();
    for (set_index, hit_set) in hit_sets.iter().enumerate() {
        let weight = options
            .weights
            .as_ref()
            .and_then(|weights| weights.get(set_index))
            .copied()
            .unwrap_or(1.0);
        for hit in hit_set {
            let dedupe_key = dedupe_key_for(hit);
            if !grouped_hits.contains_key(&dedupe_key) {
                first_seen_keys.push(dedupe_key.clone());
            }
            grouped_hits
                .entry(dedupe_key.clone())
                .or_default()
                .push(hit.clone());
            let score = match options.strategy {
                FusionStrategy::ReciprocalRankFusion => {
                    Some(weight / (options.k + hit.rank) as f64)
                }
                FusionStrategy::WeightedRank => Some(weight / hit.rank.max(1) as f64),
                FusionStrategy::NormalizedScore => {
                    Some(weight * hit.normalized_score.unwrap_or(0.0))
                }
                FusionStrategy::Concatenate | FusionStrategy::Interleave => None,
            };
            if let Some(score) = score {
                *fusion_scores.entry(dedupe_key).or_insert(0.0) += score;
            }
        }
    }

    let (ordered_keys, score_kind, max_score) = match options.strategy {
        FusionStrategy::Concatenate => (first_seen_keys, "concatenate", None),
        FusionStrategy::Interleave => {
            let mut sorted_hit_sets = hit_sets
                .iter()
                .map(|hit_set| {
                    let mut hits = hit_set.iter().collect::<Vec<_>>();
                    hits.sort_by(|left, right| {
                        left.rank
                            .cmp(&right.rank)
                            .then_with(|| left.hit_id.cmp(&right.hit_id))
                    });
                    hits
                })
                .collect::<Vec<_>>();
            let max_len = sorted_hit_sets
                .iter()
                .map(|hit_set| hit_set.len())
                .max()
                .unwrap_or(0);
            let mut keys = Vec::new();
            let mut seen_keys = BTreeSet::new();
            for index in 0..max_len {
                for hit_set in &mut sorted_hit_sets {
                    if let Some(hit) = hit_set.get(index) {
                        let dedupe_key = dedupe_key_for(hit);
                        if seen_keys.insert(dedupe_key.clone()) {
                            keys.push(dedupe_key);
                        }
                    }
                }
            }
            (keys, "interleave", None)
        }
        FusionStrategy::ReciprocalRankFusion
        | FusionStrategy::WeightedRank
        | FusionStrategy::NormalizedScore => {
            let mut keys = grouped_hits.keys().cloned().collect::<Vec<_>>();
            keys.sort_by(|left, right| {
                let left_score = fusion_scores.get(left).copied().unwrap_or(0.0);
                let right_score = fusion_scores.get(right).copied().unwrap_or(0.0);
                right_score
                    .partial_cmp(&left_score)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| {
                        let left_rank = grouped_hits[left]
                            .iter()
                            .map(|hit| hit.rank)
                            .min()
                            .unwrap_or(usize::MAX);
                        let right_rank = grouped_hits[right]
                            .iter()
                            .map(|hit| hit.rank)
                            .min()
                            .unwrap_or(usize::MAX);
                        left_rank.cmp(&right_rank)
                    })
                    .then_with(|| left.cmp(right))
            });
            let max_score = keys
                .first()
                .and_then(|dedupe_key| fusion_scores.get(dedupe_key))
                .copied();
            let score_kind = match options.strategy {
                FusionStrategy::ReciprocalRankFusion => "reciprocal_rank_fusion",
                FusionStrategy::WeightedRank => "weighted_rank",
                FusionStrategy::NormalizedScore => "normalized_score",
                FusionStrategy::Concatenate | FusionStrategy::Interleave => unreachable!(),
            };
            (keys, score_kind, max_score)
        }
    };

    let mut fused_hits = Vec::new();
    for (rank, dedupe_key) in ordered_keys.iter().enumerate() {
        let source_hits = &grouped_hits[dedupe_key];
        let representative = &source_hits[0];
        let mut metadata = representative.metadata.clone();
        metadata.insert(
            "source_hit_ids".to_owned(),
            json!(
                source_hits
                    .iter()
                    .map(|hit| hit.hit_id.clone())
                    .collect::<Vec<_>>()
            ),
        );
        let mut source_ranks = Map::new();
        for hit in source_hits {
            source_ranks.insert(hit.retriever.clone(), json!(hit.rank));
        }
        metadata.insert("source_ranks".to_owned(), Value::Object(source_ranks));
        metadata.insert(
            "fusion_strategy".to_owned(),
            json!(match options.strategy {
                FusionStrategy::Concatenate => "concatenate",
                FusionStrategy::ReciprocalRankFusion => "reciprocal_rank_fusion",
                FusionStrategy::WeightedRank => "weighted_rank",
                FusionStrategy::NormalizedScore => "normalized_score",
                FusionStrategy::Interleave => "interleave",
            }),
        );
        metadata.insert("dedupe_key".to_owned(), json!(dedupe_key));
        let raw_score = match options.strategy {
            FusionStrategy::ReciprocalRankFusion
            | FusionStrategy::WeightedRank
            | FusionStrategy::NormalizedScore => fusion_scores.get(dedupe_key).copied(),
            FusionStrategy::Concatenate | FusionStrategy::Interleave => None,
        };
        metadata.insert(
            "fusion_score".to_owned(),
            raw_score.map_or(Value::Null, Value::from),
        );
        let mut highlights = Vec::new();
        let mut seen_source_ids = BTreeSet::new();
        for hit in source_hits {
            if hit.highlights.is_empty() {
                if seen_source_ids.insert(hit.item.source.source_id.clone()) {
                    highlights.push(hit.item.source.clone());
                }
            } else {
                for source_ref in &hit.highlights {
                    if seen_source_ids.insert(source_ref.source_id.clone()) {
                        highlights.push(source_ref.clone());
                    }
                }
            }
        }
        let mut fused = SearchHit::new(
            format!("{}:{}", options.retriever_id, representative.item.item_id),
            representative.item.clone(),
            rank + 1,
            &options.retriever_id,
        )
        .with_score_kind(score_kind)
        .with_highlights(highlights);
        fused.raw_score = raw_score;
        fused.normalized_score =
            raw_score.and_then(|score| max_score.filter(|max| *max > 0.0).map(|max| score / max));
        fused.metadata = metadata;
        fused_hits.push(fused);
    }
    Ok(fused_hits)
}

fn fusion_strategy_label(strategy: &FusionStrategy) -> &'static str {
    match strategy {
        FusionStrategy::Concatenate => "concatenate",
        FusionStrategy::ReciprocalRankFusion => "reciprocal_rank_fusion",
        FusionStrategy::WeightedRank => "weighted_rank",
        FusionStrategy::NormalizedScore => "normalized_score",
        FusionStrategy::Interleave => "interleave",
    }
}

pub fn rerank_search_hits(
    mut hits: Vec<SearchHit>,
    options: RerankOptions,
) -> Result<RerankResult, RagError> {
    if matches!(options.input_limit, Some(0)) {
        return Err(RagError::InvalidRerankInputLimit);
    }
    let input_count = hits.len();
    hits.sort_by(|left, right| {
        left.rank
            .cmp(&right.rank)
            .then_with(|| left.hit_id.cmp(&right.hit_id))
    });
    let evaluated_count = options
        .input_limit
        .map_or(hits.len(), |limit| limit.min(hits.len()));
    let truncated_hit_ids = hits
        .iter()
        .skip(evaluated_count)
        .map(|hit| hit.hit_id.clone())
        .collect::<Vec<_>>();
    let mut ranked_hits = Vec::new();
    for hit in hits.into_iter().take(evaluated_count) {
        let preview_text = hit.item.preview.join("\n").to_ascii_lowercase();
        let score = options
            .query_terms
            .iter()
            .map(|term| preview_text.matches(term).count())
            .sum::<usize>();
        let mut metadata = BTreeMap::new();
        metadata.insert("original_rank".to_owned(), json!(hit.rank));
        metadata.insert("source_hit_id".to_owned(), json!(hit.hit_id));
        metadata.insert("query_terms".to_owned(), json!(options.query_terms));
        ranked_hits.push(RankedHit {
            hit,
            rerank_score: Some(score as f64),
            reranker: Some(options.reranker_id.clone()),
            explanation: Some(format!("matched {score} query term occurrence(s)")),
            metadata,
        });
    }
    ranked_hits.sort_by(|left, right| {
        right
            .rerank_score
            .partial_cmp(&left.rerank_score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.hit.rank.cmp(&right.hit.rank))
            .then_with(|| left.hit.hit_id.cmp(&right.hit.hit_id))
    });
    for (rank, ranked_hit) in ranked_hits.iter_mut().enumerate() {
        ranked_hit.hit.rank = rank + 1;
    }
    let mut metadata = BTreeMap::new();
    metadata.insert("query_terms".to_owned(), json!(options.query_terms));
    metadata.insert("truncated_hit_ids".to_owned(), json!(truncated_hit_ids));
    Ok(RerankResult {
        ranked_hits,
        reranker: options.reranker_id,
        input_count,
        evaluated_count,
        truncated_hit_ids,
        metadata,
    })
}

pub fn resolve_citation_source_trace(
    answer: &Answer,
    context: &ContextPack,
    citation_id: impl AsRef<str>,
) -> Result<CitationSourceTrace, RagError> {
    let citation_id = citation_id.as_ref();
    let Some(citation) = answer
        .citations
        .iter()
        .find(|citation| citation.citation_id == citation_id)
    else {
        return Err(RagError::CitationNotFound {
            citation_id: citation_id.to_owned(),
        });
    };
    let claim_id = answer
        .claims
        .iter()
        .find(|claim| claim.citation_ids.iter().any(|id| id == citation_id))
        .map(|claim| claim.claim_id.clone())
        .or_else(|| citation.claim_id.clone());

    for hit in &context.hits {
        for source_ref in std::iter::once(&hit.item.source).chain(hit.highlights.iter()) {
            if source_ref_matches(&citation.source, source_ref) {
                let mut element_ids = hit
                    .item
                    .metadata
                    .get("element_ids")
                    .and_then(Value::as_array)
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_owned)
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                if element_ids.is_empty()
                    && let Some(element_id) = source_ref
                        .locator
                        .as_ref()
                        .and_then(|locator| locator.element_id.clone())
                {
                    element_ids.push(element_id);
                }
                return Ok(CitationSourceTrace {
                    citation_id: citation.citation_id.clone(),
                    claim_id,
                    context_id: context.context_id.clone(),
                    hit_id: hit.hit_id.clone(),
                    retriever: hit.retriever.clone(),
                    item_id: hit.item.item_id.clone(),
                    item_kind: hit.item.item_kind.clone(),
                    source: source_ref.clone(),
                    locator: source_ref.locator.clone(),
                    acl: hit.item.acl.clone(),
                    element_ids,
                });
            }
        }
    }

    Err(RagError::CitationSourceNotInContext {
        citation_id: citation.citation_id.clone(),
    })
}

pub fn validate_answer_citations(
    answer: &Answer,
    context: &ContextPack,
    require_citations: bool,
    failure_policy: FailurePolicy,
) -> Result<CitationValidationResult, RagError> {
    let severity = if failure_policy == FailurePolicy::Warn {
        CitationSeverity::Warning
    } else {
        CitationSeverity::Error
    };
    let normalize_text = |text: &str| {
        text.split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_ascii_lowercase()
    };
    let mut issues = Vec::new();
    let mut citations_by_id: BTreeMap<String, &Citation> = BTreeMap::new();
    let mut context_source_texts: Vec<(&SourceRef, String)> = Vec::new();

    for citation in &answer.citations {
        if citations_by_id.contains_key(&citation.citation_id) {
            issues.push(
                CitationValidationIssue::new(
                    "citation_id.duplicate",
                    format!(
                        "citation {:?} is defined more than once",
                        citation.citation_id
                    ),
                    severity.clone(),
                )
                .with_citation_id(&citation.citation_id),
            );
        } else {
            citations_by_id.insert(citation.citation_id.clone(), citation);
        }
    }

    for hit in &context.hits {
        let preview_text = hit.item.preview.join("\n");
        for source_ref in std::iter::once(&hit.item.source).chain(hit.highlights.iter()) {
            context_source_texts.push((source_ref, preview_text.clone()));
        }
    }

    for claim in &answer.claims {
        if require_citations && !claim.text.trim().is_empty() && claim.citation_ids.is_empty() {
            issues.push(
                CitationValidationIssue::new(
                    "claim.missing_citation",
                    format!("claim {:?} has no citation", claim.claim_id),
                    severity.clone(),
                )
                .with_claim_id(&claim.claim_id),
            );
        }
        for citation_id in &claim.citation_ids {
            let Some(citation) = citations_by_id.get(citation_id).copied() else {
                issues.push(
                    CitationValidationIssue::new(
                        "citation_id.missing",
                        format!(
                            "claim {:?} references missing citation {:?}",
                            claim.claim_id, citation_id
                        ),
                        severity.clone(),
                    )
                    .with_citation_id(citation_id)
                    .with_claim_id(&claim.claim_id),
                );
                continue;
            };
            if citation
                .claim_id
                .as_ref()
                .is_some_and(|claim_id| claim_id != &claim.claim_id)
            {
                issues.push(
                    CitationValidationIssue::new(
                        "citation.claim_mismatch",
                        format!(
                            "citation {:?} is attached to a different claim",
                            citation.citation_id
                        ),
                        severity.clone(),
                    )
                    .with_citation_id(&citation.citation_id)
                    .with_claim_id(&claim.claim_id),
                );
            }
            let normalized_claim_text = normalize_text(&claim.text);
            if !normalized_claim_text.is_empty() {
                let matching_texts = context_source_texts
                    .iter()
                    .filter_map(|(source_ref, text)| {
                        if source_ref_matches(&citation.source, source_ref) {
                            Some(text.as_str())
                        } else {
                            None
                        }
                    })
                    .collect::<Vec<_>>();
                if !matching_texts.is_empty()
                    && !matching_texts
                        .iter()
                        .any(|text| normalize_text(text).contains(&normalized_claim_text))
                {
                    issues.push(
                        CitationValidationIssue::new(
                            "claim.unsupported_by_citation",
                            format!(
                                "claim {:?} is not supported by citation {:?}",
                                claim.claim_id, citation.citation_id
                            ),
                            severity.clone(),
                        )
                        .with_citation_id(&citation.citation_id)
                        .with_claim_id(&claim.claim_id),
                    );
                }
            }
        }
    }

    for citation in &answer.citations {
        let matching_sources = context_source_texts
            .iter()
            .filter(|(source_ref, _)| source_ref_matches(&citation.source, source_ref))
            .collect::<Vec<_>>();
        if matching_sources.is_empty() {
            issues.push(
                CitationValidationIssue::new(
                    "citation.source_not_in_context",
                    format!(
                        "citation {:?} does not point to the current context",
                        citation.citation_id
                    ),
                    severity.clone(),
                )
                .with_citation_id(&citation.citation_id),
            );
            continue;
        }
        if citation.source.locator.is_none()
            && matching_sources
                .iter()
                .any(|(source_ref, _)| source_ref.locator.is_none())
        {
            issues.push(
                CitationValidationIssue::new(
                    "citation.precision_limited",
                    format!(
                        "citation {:?} has no page, cell, slide, or span locator",
                        citation.citation_id
                    ),
                    CitationSeverity::Warning,
                )
                .with_citation_id(&citation.citation_id),
            );
        }
        if let Some(cited_text) = &citation.cited_text {
            let quoted_text = normalize_text(cited_text);
            if !quoted_text.is_empty()
                && !matching_sources
                    .iter()
                    .any(|(_, text)| normalize_text(text).contains(&quoted_text))
            {
                issues.push(
                    CitationValidationIssue::new(
                        "citation.text_mismatch",
                        format!(
                            "citation {:?} cites text outside the source preview",
                            citation.citation_id
                        ),
                        severity.clone(),
                    )
                    .with_citation_id(&citation.citation_id),
                );
            }
        }
    }

    if issues.is_empty() {
        return Ok(CitationValidationResult::ok());
    }
    if !issues
        .iter()
        .any(|issue| issue.severity == CitationSeverity::Error)
    {
        return Ok(CitationValidationResult {
            ok: true,
            issues,
            abstention: None,
            repaired_answer: None,
        });
    }
    if failure_policy == FailurePolicy::Warn {
        return Ok(CitationValidationResult {
            ok: true,
            issues,
            abstention: None,
            repaired_answer: None,
        });
    }
    if matches!(
        failure_policy,
        FailurePolicy::RemoveInvalid | FailurePolicy::Repair
    ) {
        let invalid_citation_ids = issues
            .iter()
            .filter(|issue| issue.severity == CitationSeverity::Error)
            .filter_map(|issue| issue.citation_id.clone())
            .collect::<BTreeSet<_>>();
        let mut repaired_answer = answer.clone();
        repaired_answer
            .citations
            .retain(|citation| !invalid_citation_ids.contains(&citation.citation_id));
        let remaining_citation_ids = repaired_answer
            .citations
            .iter()
            .map(|citation| citation.citation_id.clone())
            .collect::<BTreeSet<_>>();
        for claim in &mut repaired_answer.claims {
            claim
                .citation_ids
                .retain(|citation_id| remaining_citation_ids.contains(citation_id));
        }
        if failure_policy == FailurePolicy::RemoveInvalid {
            return Ok(CitationValidationResult {
                ok: true,
                issues,
                abstention: None,
                repaired_answer: Some(repaired_answer),
            });
        }
        if failure_policy == FailurePolicy::Repair {
            let repaired_result = validate_answer_citations(
                &repaired_answer,
                context,
                require_citations,
                FailurePolicy::Fail,
            )?;
            let repaired_ok = repaired_result.ok;
            for repaired_issue in repaired_result.issues {
                if !issues.iter().any(|issue| {
                    issue.code == repaired_issue.code
                        && issue.citation_id == repaired_issue.citation_id
                        && issue.claim_id == repaired_issue.claim_id
                }) {
                    issues.push(repaired_issue);
                }
            }
            return Ok(CitationValidationResult {
                ok: repaired_ok,
                issues,
                abstention: None,
                repaired_answer: Some(repaired_answer),
            });
        }
    }
    if failure_policy == FailurePolicy::Abstain {
        let mut diagnostics = BTreeMap::new();
        diagnostics.insert(
            "issue_codes".to_owned(),
            json!(issues.iter().map(|issue| &issue.code).collect::<Vec<_>>()),
        );
        return Ok(CitationValidationResult {
            ok: false,
            issues,
            abstention: Some(Abstention {
                reason: "citation_validation_failed".to_owned(),
                user_message: "I do not have enough validated source support to answer.".to_owned(),
                diagnostics,
            }),
            repaired_answer: None,
        });
    }
    Ok(CitationValidationResult {
        ok: false,
        issues,
        abstention: None,
        repaired_answer: None,
    })
}

pub fn validate_answer_grounding(
    answer: &Answer,
    context: &ContextPack,
    require_citations: bool,
    failure_policy: FailurePolicy,
) -> Result<CitationValidationResult, RagError> {
    if context.hits.is_empty()
        && (!answer.text.trim().is_empty()
            || answer
                .claims
                .iter()
                .any(|claim| !claim.text.trim().is_empty()))
    {
        let severity = if failure_policy == FailurePolicy::Warn {
            CitationSeverity::Warning
        } else {
            CitationSeverity::Error
        };
        let issues = vec![CitationValidationIssue::new(
            "grounding.insufficient_context",
            "answer grounding requires at least one context hit",
            severity,
        )];
        if failure_policy == FailurePolicy::Warn {
            return Ok(CitationValidationResult {
                ok: true,
                issues,
                abstention: None,
                repaired_answer: None,
            });
        }
        if failure_policy == FailurePolicy::Abstain {
            let mut diagnostics = BTreeMap::new();
            diagnostics.insert(
                "issue_codes".to_owned(),
                json!(["grounding.insufficient_context"]),
            );
            return Ok(CitationValidationResult {
                ok: false,
                issues,
                abstention: Some(Abstention {
                    reason: "insufficient_context".to_owned(),
                    user_message: "I do not have enough retrieved context to answer.".to_owned(),
                    diagnostics,
                }),
                repaired_answer: None,
            });
        }
        return Ok(CitationValidationResult {
            ok: false,
            issues,
            abstention: None,
            repaired_answer: None,
        });
    }

    validate_answer_citations(answer, context, require_citations, failure_policy)
}

pub fn validate_answer_citation_authorization(
    answer: &Answer,
    context: &ContextPack,
    auth: Option<&AuthContext>,
) -> Result<CitationValidationResult, RagError> {
    let mut issues = Vec::new();
    for citation in &answer.citations {
        let mut matched_context_source = false;
        let mut has_authorized_source = false;
        for hit in &context.hits {
            for source_ref in std::iter::once(&hit.item.source).chain(hit.highlights.iter()) {
                if source_ref_matches(&citation.source, source_ref) {
                    matched_context_source = true;
                    if acl_allows(&hit.hit_id, &hit.item.acl, auth)? {
                        has_authorized_source = true;
                    }
                }
            }
        }
        if !matched_context_source {
            issues.push(
                CitationValidationIssue::new(
                    "citation.source_not_in_context",
                    format!(
                        "citation {:?} does not point to the current context",
                        citation.citation_id
                    ),
                    CitationSeverity::Error,
                )
                .with_citation_id(&citation.citation_id),
            );
        } else if !has_authorized_source {
            issues.push(
                CitationValidationIssue::new(
                    "citation.source_not_authorized",
                    format!(
                        "citation {:?} points to a source outside the principal authorization scope",
                        citation.citation_id
                    ),
                    CitationSeverity::Error,
                )
                .with_citation_id(&citation.citation_id),
            );
        }
    }
    Ok(CitationValidationResult {
        ok: issues.is_empty(),
        issues,
        abstention: None,
        repaired_answer: None,
    })
}

fn source_ref_matches(citation_source: &SourceRef, context_source: &SourceRef) -> bool {
    citation_source.source_id == context_source.source_id
        && citation_source
            .revision
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_source.revision.as_ref())
        && citation_source
            .digest
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_source.digest.as_ref())
        && locator_matches(&citation_source.locator, &context_source.locator)
}

fn locator_matches(
    citation_locator: &Option<DocumentSpan>,
    context_locator: &Option<DocumentSpan>,
) -> bool {
    let Some(citation_locator) = citation_locator else {
        return true;
    };
    let Some(context_locator) = context_locator else {
        return false;
    };
    citation_locator.asset_id == context_locator.asset_id
        && citation_locator.revision_id == context_locator.revision_id
        && citation_locator.document_id == context_locator.document_id
        && citation_locator
            .element_id
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_locator.element_id.as_ref())
        && citation_locator
            .chunk_id
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_locator.chunk_id.as_ref())
        && citation_locator
            .page
            .is_none_or(|expected| Some(expected) == context_locator.page)
        && citation_locator
            .bbox
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_locator.bbox.as_ref())
        && citation_locator
            .char_start
            .is_none_or(|expected| Some(expected) == context_locator.char_start)
        && citation_locator
            .char_end
            .is_none_or(|expected| Some(expected) == context_locator.char_end)
        && citation_locator
            .sheet
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_locator.sheet.as_ref())
        && citation_locator
            .cell_range
            .as_ref()
            .is_none_or(|expected| Some(expected) == context_locator.cell_range.as_ref())
        && citation_locator
            .slide
            .is_none_or(|expected| Some(expected) == context_locator.slide)
}

fn acl_allows(
    resource_id: &str,
    acl: &Option<Value>,
    auth: Option<&AuthContext>,
) -> Result<bool, RagError> {
    let Some(acl) = acl else {
        return Ok(true);
    };
    if acl.is_null() {
        return Ok(true);
    }
    let Some(acl) = acl.as_object() else {
        return Err(RagError::InvalidAcl {
            resource_id: resource_id.to_owned(),
            message: "ACL must be an object".to_owned(),
        });
    };
    if acl.get("public").and_then(Value::as_bool) == Some(true) {
        return Ok(true);
    }
    let Some(auth) = auth else {
        return Err(RagError::AuthContextRequired {
            resource_id: resource_id.to_owned(),
        });
    };
    if let Some(tenant_id) = acl.get("tenant_id").and_then(Value::as_str)
        && tenant_id != auth.tenant_id
    {
        return Ok(false);
    }

    let mut has_selector = false;
    if let Some(principals) = acl.get("principals") {
        let Some(principals) = principals.as_array() else {
            return Err(RagError::InvalidAcl {
                resource_id: resource_id.to_owned(),
                message: "principals must be an array".to_owned(),
            });
        };
        has_selector = true;
        for principal in principals {
            let Some(principal) = principal.as_str() else {
                return Err(RagError::InvalidAcl {
                    resource_id: resource_id.to_owned(),
                    message: "principals entries must be strings".to_owned(),
                });
            };
            if principal == auth.principal_id {
                return Ok(true);
            }
        }
    }
    if let Some(groups) = acl.get("groups") {
        let Some(groups) = groups.as_array() else {
            return Err(RagError::InvalidAcl {
                resource_id: resource_id.to_owned(),
                message: "groups must be an array".to_owned(),
            });
        };
        has_selector = true;
        for group in groups {
            let Some(group) = group.as_str() else {
                return Err(RagError::InvalidAcl {
                    resource_id: resource_id.to_owned(),
                    message: "groups entries must be strings".to_owned(),
                });
            };
            if auth.groups.contains(group) {
                return Ok(true);
            }
        }
    }
    if let Some(roles) = acl.get("roles") {
        let Some(roles) = roles.as_array() else {
            return Err(RagError::InvalidAcl {
                resource_id: resource_id.to_owned(),
                message: "roles must be an array".to_owned(),
            });
        };
        has_selector = true;
        for role in roles {
            let Some(role) = role.as_str() else {
                return Err(RagError::InvalidAcl {
                    resource_id: resource_id.to_owned(),
                    message: "roles entries must be strings".to_owned(),
                });
            };
            if auth.roles.contains(role) {
                return Ok(true);
            }
        }
    }
    if let Some(attributes) = acl.get("attributes") {
        let Some(attributes) = attributes.as_object() else {
            return Err(RagError::InvalidAcl {
                resource_id: resource_id.to_owned(),
                message: "attributes must be an object".to_owned(),
            });
        };
        has_selector = true;
        if attributes
            .iter()
            .all(|(name, expected)| auth.attributes.get(name) == Some(expected))
        {
            return Ok(true);
        }
    }
    Ok(!has_selector)
}
