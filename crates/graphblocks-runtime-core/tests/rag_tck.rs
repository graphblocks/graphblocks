#![allow(clippy::panic)]

use graphblocks_runtime_core::documents::{DocumentSpan, SourceRef};
use graphblocks_runtime_core::rag::{
    Answer, Citation, Claim, ContextPack, FailurePolicy, KnowledgeItemRef, SearchHit,
    validate_answer_grounding,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[test]
fn rust_rag_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/rag/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "rag TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let name = required_str(case, "name", "rag TCK case")?;
    let kind = required_str(case, "kind", name)?;
    if kind != "grounding" {
        return Err(format!("rag TCK case {name} has unsupported kind {kind}"));
    }
    let context_fixture = case
        .get("context")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("rag TCK case {name} is missing context"))?;
    let context_id = context_fixture
        .get("contextId")
        .and_then(Value::as_str)
        .unwrap_or("ctx-1");
    let context = if let Some(text) = context_fixture.get("text").and_then(Value::as_str) {
        ContextPack::new(context_id, vec![hit("hit-1", "chunk-1", "doc-1", text)])
    } else {
        ContextPack::new(context_id, Vec::new())
    };

    let answer_fixture = case
        .get("answer")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("rag TCK case {name} is missing answer"))?;
    let mut answer = Answer::new(
        answer_fixture
            .get("answerId")
            .and_then(Value::as_str)
            .unwrap_or("answer-1"),
        answer_fixture
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or(""),
    );
    if let Some(claims) = answer_fixture.get("claims").and_then(Value::as_array) {
        for claim in claims {
            let claim_id = required_str(claim, "claimId", name)?;
            let text = required_str(claim, "text", name)?;
            let citation_ids = claim
                .get("citationIds")
                .and_then(Value::as_array)
                .map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>())
                .unwrap_or_default();
            answer = answer.with_claim(Claim::new(claim_id, text).with_citation_ids(citation_ids));
        }
    }
    if let Some(citations) = answer_fixture.get("citations").and_then(Value::as_array) {
        for citation in citations {
            let citation_id = required_str(citation, "citationId", name)?;
            let source_hit_index = citation
                .get("sourceHitIndex")
                .and_then(Value::as_u64)
                .ok_or_else(|| format!("rag TCK case {name} citation is missing sourceHitIndex"))?
                as usize;
            let source = context
                .hits
                .get(source_hit_index)
                .ok_or_else(|| {
                    format!("rag TCK case {name} citation sourceHitIndex is out of range")
                })?
                .item
                .source
                .clone();
            let mut built = Citation::new(citation_id, source);
            if let Some(cited_text) = citation.get("citedText").and_then(Value::as_str) {
                built = built.with_cited_text(cited_text);
            }
            if let Some(claim_id) = citation.get("claimId").and_then(Value::as_str) {
                built.claim_id = Some(claim_id.to_owned());
            }
            answer = answer.with_citation(built);
        }
    }

    let result = validate_answer_grounding(&answer, &context, true, FailurePolicy::Abstain)
        .map_err(|error| format!("rag TCK case {name} failed validation: {error:?}"))?;
    let issue_codes = result
        .issues
        .iter()
        .map(|issue| Value::String(issue.code.clone()))
        .collect::<Vec<_>>();
    let expected = case
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("rag TCK case {name} is missing expected result"))?;

    for (key, expected_value) in expected {
        let observed = match key.as_str() {
            "ok" => json!(result.ok),
            "issueCodes" => Value::Array(issue_codes.clone()),
            "abstentionReason" => result
                .abstention
                .as_ref()
                .map(|abstention| Value::String(abstention.reason.clone()))
                .unwrap_or(Value::Null),
            unsupported => panic!("{name}: unsupported rag expectation {unsupported}"),
        };
        assert_eq!(observed, *expected_value, "{name}: expected {key} to match");
    }

    Ok(())
}

fn hit(hit_id: &str, item_id: &str, document_id: &str, preview: &str) -> SearchHit {
    let source = SourceRef::document_chunk(
        item_id,
        "rev-1",
        "sha256:content",
        DocumentSpan::new("asset-1", "rev-1", document_id).with_chunk_id(item_id),
    );
    let mut metadata = BTreeMap::new();
    metadata.insert("document_id".to_owned(), json!(document_id));
    SearchHit::new(
        hit_id,
        KnowledgeItemRef::new(item_id, "document_chunk", source.clone())
            .with_preview([preview])
            .with_metadata(metadata),
        1,
        "local-test",
    )
    .with_normalized_score(1.0)
    .with_highlights([source])
}

fn required_str<'a>(value: &'a Value, key: &str, owner: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{owner} is missing string field {key}"))
}
