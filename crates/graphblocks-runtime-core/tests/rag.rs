use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::documents::{
    DocumentSpan, SourceRef, chunk_document_by_lines, create_local_text_revision,
    parse_plain_text_document,
};
use graphblocks_runtime_core::rag::{
    Answer, Citation, Claim, ContextBuildOptions, FailurePolicy, FusionOptions, FusionStrategy,
    InMemoryChunkRetriever, KnowledgeItemRef, SearchHit, SearchRequest, build_context_pack,
    fuse_search_hits, validate_answer_citations,
};
use serde_json::json;

fn hit(hit_id: &str, item_id: &str, document_id: &str, preview: &str, rank: usize) -> SearchHit {
    hit_from(hit_id, item_id, document_id, preview, rank, "local")
}

fn hit_from(
    hit_id: &str,
    item_id: &str,
    document_id: &str,
    preview: &str,
    rank: usize,
    retriever: &str,
) -> SearchHit {
    let source = SourceRef::document_chunk(
        item_id,
        "rev-1",
        "sha256:content",
        DocumentSpan::new("asset-1", "rev-1", document_id).with_chunk_id(item_id),
    );
    let mut metadata = std::collections::BTreeMap::new();
    metadata.insert("document_id".to_owned(), json!(document_id));
    SearchHit::new(
        hit_id,
        KnowledgeItemRef::new(item_id, "document_chunk", source.clone())
            .with_preview([preview])
            .with_metadata(metadata),
        rank,
        retriever,
    )
    .with_normalized_score(1.0 / rank as f64)
    .with_highlights([source])
}

#[test]
fn in_memory_chunk_retriever_returns_ranked_hits_with_lineage() {
    let (asset, revision) = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta beta\nbeta gamma\nunrelated\n",
        "2026-06-22T00:00:00Z",
        Some("notes.txt"),
    );
    let document = parse_plain_text_document(
        &asset,
        &revision,
        "alpha beta beta\nbeta gamma\nunrelated\n",
    );
    let chunks = chunk_document_by_lines(&document, &revision, 1).expect("chunking succeeds");
    let retriever = InMemoryChunkRetriever::new(chunks.clone(), "local-test");
    let request = SearchRequest::new("beta").with_top_k(2);

    let result = retriever.retrieve(request.clone());

    let request_hash = canonical_hash(&json!({
        "filters": {},
        "query_text": "beta",
        "top_k": 2,
    }));
    assert_eq!(result.retrieval_id, format!("local-test:{request_hash}"));
    assert_eq!(result.request, request);
    assert_eq!(result.total_candidates, Some(2));
    assert_eq!(
        result
            .hits
            .iter()
            .map(|hit| hit.item.item_id.as_str())
            .collect::<Vec<_>>(),
        vec![chunks[0].chunk_id.as_str(), chunks[1].chunk_id.as_str()]
    );
    assert_eq!(
        result.hits.iter().map(|hit| hit.rank).collect::<Vec<_>>(),
        vec![1, 2]
    );
    assert_eq!(result.hits[0].raw_score, Some(2.0));
    assert_eq!(result.hits[1].normalized_score, Some(0.5));
    assert_eq!(result.hits[0].highlights[0], chunks[0].source_refs[0]);
}

#[test]
fn build_context_pack_respects_token_budget_and_records_drop_reasons() {
    let hits = vec![
        hit("hit-1", "chunk-1", "doc-1", "alpha beta", 1),
        hit("hit-2", "chunk-2", "doc-2", "gamma delta", 2),
        hit("hit-3", "chunk-3", "doc-3", "epsilon", 3),
    ];

    let context = build_context_pack("ctx-1", hits, ContextBuildOptions::new(3))
        .expect("context build succeeds");

    assert_eq!(
        context
            .hits
            .iter()
            .map(|hit| hit.hit_id.as_str())
            .collect::<Vec<_>>(),
        vec!["hit-1", "hit-3"]
    );
    assert_eq!(context.token_budget, Some(3));
    assert_eq!(context.token_count, Some(3));
    assert_eq!(
        context.metadata["selected_hit_ids"],
        json!(["hit-1", "hit-3"])
    );
    assert_eq!(context.metadata["dropped_hit_ids"], json!(["hit-2"]));
    assert_eq!(
        context.metadata["drop_reasons"],
        json!({"hit-2": "token_budget"})
    );
}

#[test]
fn build_context_pack_limits_per_document_and_deduplicates_items() {
    let first = hit("hit-1", "chunk-1", "doc-1", "alpha", 1);
    let same_document = hit("hit-2", "chunk-2", "doc-1", "beta", 2);
    let duplicate = hit("hit-3", "chunk-1", "doc-1", "alpha", 3);

    let context = build_context_pack(
        "ctx-1",
        vec![first, same_document, duplicate],
        ContextBuildOptions::new(10).with_per_document_max_chunks(1),
    )
    .expect("context build succeeds");

    assert_eq!(
        context
            .hits
            .iter()
            .map(|hit| hit.hit_id.as_str())
            .collect::<Vec<_>>(),
        vec!["hit-1"]
    );
    assert_eq!(
        context.metadata["dropped_hit_ids"],
        json!(["hit-2", "hit-3"])
    );
    assert_eq!(
        context.metadata["drop_reasons"],
        json!({
            "hit-2": "per_document_max_chunks",
            "hit-3": "duplicate",
        })
    );
}

#[test]
fn fuse_search_hits_uses_reciprocal_rank_fusion_and_preserves_source_ranks() {
    let keyword_hits = vec![
        hit_from("kw-b", "chunk-b", "doc-1", "chunk-b", 1, "keyword"),
        hit_from("kw-a", "chunk-a", "doc-1", "chunk-a", 2, "keyword"),
    ];
    let dense_hits = vec![hit_from(
        "dense-a", "chunk-a", "doc-1", "chunk-a", 1, "dense",
    )];

    let fused = fuse_search_hits(
        &[keyword_hits, dense_hits],
        FusionOptions::new()
            .with_strategy(FusionStrategy::ReciprocalRankFusion)
            .with_k(60)
            .with_retriever_id("fused"),
    )
    .expect("fusion succeeds");

    assert_eq!(
        fused
            .iter()
            .map(|hit| hit.item.item_id.as_str())
            .collect::<Vec<_>>(),
        vec!["chunk-a", "chunk-b"]
    );
    assert_eq!(fused[0].rank, 1);
    assert_eq!(fused[0].retriever, "fused");
    assert_eq!(
        fused[0].score_kind.as_deref(),
        Some("reciprocal_rank_fusion")
    );
    assert_eq!(
        fused[0].metadata["source_hit_ids"],
        json!(["kw-a", "dense-a"])
    );
    assert_eq!(
        fused[0].metadata["source_ranks"],
        json!({"dense": 1, "keyword": 2})
    );
    assert_eq!(fused[0].normalized_score, Some(1.0));
}

#[test]
fn validate_answer_citations_abstains_when_cited_text_is_not_in_context() {
    let context = build_context_pack(
        "ctx-1",
        vec![hit(
            "hit-1",
            "chunk-1",
            "doc-1",
            "Alpha policy requires audit logs.",
            1,
        )],
        ContextBuildOptions::new(10),
    )
    .expect("context build succeeds");
    let citation = Citation::new("cite-1", context.hits[0].item.source.clone())
        .with_cited_text("unrelated phrase");
    let answer = Answer::new("answer-1", "Alpha policy requires audit logs.")
        .with_claim(
            Claim::new("claim-1", "Alpha policy requires audit logs.")
                .with_citation_ids(["cite-1"]),
        )
        .with_citation(citation);

    let result = validate_answer_citations(&answer, &context, true, FailurePolicy::Abstain)
        .expect("citation validation succeeds");

    assert!(!result.ok);
    assert_eq!(
        result
            .issues
            .iter()
            .map(|issue| issue.code.as_str())
            .collect::<Vec<_>>(),
        vec!["citation.text_mismatch"]
    );
    assert_eq!(
        result.abstention.as_ref().map(|item| item.reason.as_str()),
        Some("citation_validation_failed")
    );
}

#[test]
fn validate_answer_citations_accepts_current_context_source() {
    let context = build_context_pack(
        "ctx-1",
        vec![hit(
            "hit-1",
            "chunk-1",
            "doc-1",
            "Alpha policy requires audit logs.",
            1,
        )],
        ContextBuildOptions::new(10),
    )
    .expect("context build succeeds");
    let citation = Citation::new("cite-1", context.hits[0].item.source.clone())
        .with_cited_text("requires audit logs");
    let answer = Answer::new("answer-1", "Alpha policy requires audit logs.")
        .with_claim(
            Claim::new("claim-1", "Alpha policy requires audit logs.")
                .with_citation_ids(["cite-1"]),
        )
        .with_citation(citation);

    let result = validate_answer_citations(&answer, &context, true, FailurePolicy::Fail)
        .expect("citation validation succeeds");

    assert!(result.ok);
    assert!(result.issues.is_empty());
    assert!(result.abstention.is_none());
}
