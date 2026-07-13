use std::error::Error;

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::stdlib_blocks::{
    AnswerValue, ContextBuild, ContextBuildConfig, ContextBuildInputs, FederatedSourcesValue,
    GroundingFailurePolicy, RankDocuments, RankDocumentsConfig, RankDocumentsInputs,
    RetrievalFusionAlgorithm, RetrieveExecutePlan, RetrieveExecutePlanConfig,
    RetrieveExecutePlanInputs, RetrieveFuse, RetrieveFuseConfig, RetrieveFuseInputs,
    SearchRequestValue, StructuredGenerate, StructuredGenerateConfig, StructuredGenerateInputs,
    ValidateGrounding, ValidateGroundingConfig, ValidateGroundingInputs,
};
use graphblocks_runtime_core::stdlib_runtime::{
    StdlibRunOptions, StdlibRunStatus, run_stdlib_graph_with_options,
};
use graphblocks_runtime_core::typed_graph::{GraphBuilder, GraphDocument};
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

fn graph() -> Result<GraphDocument, Box<dyn Error>> {
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
    let mut graph = GraphBuilder::new("enterprise-rag-runtime-parity")?;
    let query = graph.input::<SearchRequestValue>("query")?;
    let sources = graph.input::<FederatedSourcesValue>("sources")?;
    let retrieve = graph.add(
        "retrieve",
        RetrieveExecutePlan::new(RetrieveExecutePlanConfig::new(2, 5)?),
        RetrieveExecutePlanInputs {
            query: query.clone(),
            sources,
        },
    )?;
    let fuse = graph.add(
        "fuse",
        RetrieveFuse::new(RetrieveFuseConfig::new(
            RetrievalFusionAlgorithm::ReciprocalRankFusion,
            60,
        )?),
        RetrieveFuseInputs {
            sources: retrieve.sources,
        },
    )?;
    let rerank = graph.add(
        "rerank",
        RankDocuments::new(RankDocumentsConfig::new("deterministic-lexical")?),
        RankDocumentsInputs {
            query,
            hits: fuse.hits,
        },
    )?;
    let context = graph.add(
        "context",
        ContextBuild::new(ContextBuildConfig::new("context-key-rotation", 1_000, 100)?),
        ContextBuildInputs {
            evidence: rerank.hits,
        },
    )?;
    let generate = graph.add(
        "generate",
        StructuredGenerate::<AnswerValue>::new(StructuredGenerateConfig::new(answer)?),
        StructuredGenerateInputs {
            context: context.pack.clone(),
        },
    )?;
    let validate = graph.add(
        "validate",
        ValidateGrounding::new(ValidateGroundingConfig::new(
            true,
            GroundingFailurePolicy::Abstain,
        )),
        ValidateGroundingInputs {
            response: generate.response,
            context: context.pack,
        },
    )?;
    graph.bind_output("candidate", &validate.candidate)?;
    graph.bind_output("validation", &validate.validation)?;
    Ok(graph.build())
}

fn execute() -> Result<Value, Box<dyn Error>> {
    let graph = graph()?;
    let inputs: Value = serde_json::from_str(include_str!("../../1-1-yaml-runtime/inputs.json"))?;
    let result = run_stdlib_graph_with_options(
        &graph,
        &inputs,
        &StdlibRunOptions::default().with_run_id("example-01-3-rust"),
    )?;
    if result.status != StdlibRunStatus::Succeeded {
        return Err("Rust runtime did not succeed".into());
    }
    let candidate = result
        .outputs
        .get("candidate")
        .ok_or("Rust runtime did not produce a candidate")?;
    let validation = result
        .outputs
        .get("validation")
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
        .journal
        .iter()
        .filter(|record| record.get("kind").and_then(Value::as_str) == Some("node_completed"))
        .filter_map(|record| {
            record
                .get("nodeId")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .collect::<Vec<_>>();
    let evidence = json!({
        "graphHash": result.graph_hash,
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
