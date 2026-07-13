use std::error::Error;

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_with_options_json;
use serde_json::{Value, json};

fn source_ref(
    source_id: &str,
    asset_id: &str,
    document_id: &str,
    element_id: &str,
    chunk_id: &str,
) -> Value {
    json!({
        "sourceId": source_id,
        "sourceKind": "document_chunk",
        "revision": "revision-1",
        "digest": null,
        "locator": {
            "assetId": asset_id,
            "revisionId": "revision-1",
            "documentId": document_id,
            "elementId": element_id,
            "chunkId": chunk_id,
            "page": null,
            "bbox": null,
            "charStart": null,
            "charEnd": null,
            "sheet": null,
            "cellRange": null,
            "slide": null
        },
        "observedAt": null,
        "relevantAsOf": null,
        "trust": "verified",
        "accessPolicy": null,
        "metadata": {}
    })
}

fn graph() -> Value {
    let rotation_source = source_ref(
        "source-chunk-rotation",
        "asset-handbook",
        "security-handbook",
        "paragraph-rotation",
        "chunk-rotation",
    );
    let ticket_source = source_ref(
        "source-chunk-ticket",
        "asset-tickets",
        "support-tickets",
        "ticket-approvals",
        "chunk-ticket",
    );
    let answer = json!({
        "answerId": "answer-key-rotation",
        "text": "Use the security console and obtain two approvals.",
        "claims": [
            {
                "claimId": "claim-console",
                "text": "Rotate through the security console.",
                "citationIds": ["citation-rotation"]
            },
            {
                "claimId": "claim-approvals",
                "text": "Require two approvers.",
                "citationIds": ["citation-ticket"]
            }
        ],
        "citations": [
            {
                "citationId": "citation-rotation",
                "claimId": "claim-console",
                "source": rotation_source,
                "citedText": "Rotate through the security console."
            },
            {
                "citationId": "citation-ticket",
                "claimId": "claim-approvals",
                "source": ticket_source,
                "citedText": "Require two approvers."
            }
        ]
    });
    json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "enterprise-rag-runtime-parity"},
        "spec": {
            "interface": {
                "inputs": {
                    "query": "graphblocks.ai/SearchRequest@1",
                    "sources": "graphblocks.ai/FederatedSources@1"
                },
                "outputs": {
                    "candidate": "graphblocks.ai/Answer@1",
                    "validation": "graphblocks.ai/GroundingValidation@1"
                }
            },
            "nodes": {
                "retrieve": {
                    "block": "retrieve.execute_plan@1",
                    "inputs": {"query": "$input.query", "sources": "$input.sources"},
                    "config": {"minimumSuccessfulSources": 2, "topK": 5}
                },
                "fuse": {
                    "block": "retrieve.fuse@1",
                    "inputs": {"sources": "retrieve.sources"},
                    "config": {"algorithm": "reciprocal_rank_fusion", "k": 60}
                },
                "rerank": {
                    "block": "rank.documents@1",
                    "inputs": {"query": "$input.query", "hits": "fuse.hits"},
                    "config": {"rerankerId": "deterministic-lexical"}
                },
                "context": {
                    "block": "context.build@1",
                    "inputs": {"evidence": "rerank.hits"},
                    "config": {
                        "contextId": "context-key-rotation",
                        "maxTokens": 1000,
                        "reserveOutputTokens": 100
                    }
                },
                "generate": {
                    "block": "model.structured_generate@1",
                    "inputs": {"context": "context.pack"},
                    "config": {
                        "outputSchema": "graphblocks.ai/Answer@1",
                        "response": answer
                    }
                },
                "validate": {
                    "block": "answer.validate_grounding@1",
                    "inputs": {
                        "response": "generate.response",
                        "context": "context.pack"
                    },
                    "config": {
                        "requireCitation": true,
                        "onInsufficientEvidence": "abstain"
                    },
                    "outputs": {
                        "candidate": "$output.candidate",
                        "validation": "$output.validation"
                    }
                }
            }
        }
    })
}

fn execute() -> Result<Value, Box<dyn Error>> {
    let graph = graph();
    let inputs: Value = serde_json::from_str(include_str!("../../1-1-yaml-runtime/inputs.json"))?;
    let result_json = run_stdlib_graph_with_options_json(
        &serde_json::to_string(&graph)?,
        &serde_json::to_string(&inputs)?,
        r#"{"runId":"example-01-3-rust"}"#,
    )?;
    let result: Value = serde_json::from_str(&result_json)?;
    if result.get("status").and_then(Value::as_str) != Some("succeeded") {
        return Err("Rust runtime did not succeed".into());
    }
    let candidate = result
        .pointer("/outputs/candidate")
        .ok_or("Rust runtime did not produce a candidate")?;
    let validation = result
        .pointer("/outputs/validation")
        .ok_or("Rust runtime did not produce grounding validation")?;
    if validation.get("ok").and_then(Value::as_bool) != Some(true)
        || validation
            .get("issues")
            .and_then(Value::as_array)
            .is_none_or(|issues| !issues.is_empty())
    {
        return Err("Rust runtime did not produce valid grounding evidence".into());
    }
    let citation_ids = candidate
        .get("citations")
        .and_then(Value::as_array)
        .ok_or("Rust candidate citations must be an array")?
        .iter()
        .map(|citation| {
            citation
                .get("citationId")
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or("Rust candidate citationId must be a string")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let semantic_result = json!({
        "answerId": candidate.get("answerId"),
        "citations": citation_ids,
        "status": "grounded",
        "text": candidate.get("text")
    });
    let expected = json!({
        "answerId": "answer-key-rotation",
        "citations": ["citation-rotation", "citation-ticket"],
        "status": "grounded",
        "text": "Use the security console and obtain two approvals."
    });
    if semantic_result != expected {
        return Err("Rust semantic result does not match the example contract".into());
    }
    let succeeded_nodes = result
        .get("journal")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
        .filter_map(|record| {
            record
                .get("nodeId")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .collect::<Vec<_>>();
    let evidence = json!({
        "graphHash": result.get("graphHash"),
        "grounding": {"issueCount": 0, "ok": true},
        "runtime": "rust-api",
        "semanticResult": semantic_result,
        "status": "succeeded",
        "succeededNodes": succeeded_nodes
    });
    let mut report = evidence.clone();
    report["evidenceDigest"] = Value::String(canonical_hash(&evidence));
    Ok(report)
}

fn main() -> Result<(), Box<dyn Error>> {
    println!("{}", serde_json::to_string(&execute()?)?);
    Ok(())
}
