use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::stdlib_blocks::stdlib_block_catalog;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::{Map, Value, json};

fn run_block(
    block: &str,
    inputs: Value,
    config: Value,
    output_ports: &[&str],
) -> Result<Value, String> {
    let inputs_object = inputs
        .as_object()
        .ok_or_else(|| "test inputs must be an object".to_owned())?;
    let catalog = stdlib_block_catalog().map_err(|error| error.to_string())?;
    let descriptor = catalog
        .get(block)
        .ok_or_else(|| format!("missing stdlib descriptor for {block}"))?;
    let mut interface_inputs = Map::new();
    for name in inputs_object.keys() {
        let type_ref = descriptor
            .inputs
            .iter()
            .find(|port| port.name == *name)
            .and_then(|port| port.type_ref.as_deref())
            .ok_or_else(|| format!("missing stdlib input descriptor for {block}.{name}"))?;
        interface_inputs.insert(
            name.clone(),
            json!(if type_ref == "Any" {
                "graphblocks.ai/JsonValue@1"
            } else {
                type_ref
            }),
        );
    }
    let node_inputs = inputs_object
        .keys()
        .map(|name| (name.clone(), json!(format!("$input.{name}"))))
        .collect::<Map<_, _>>();
    let mut interface_outputs = Map::new();
    for name in output_ports {
        let type_ref = descriptor
            .outputs
            .iter()
            .find(|port| port.name == *name)
            .and_then(|port| port.type_ref.as_deref())
            .ok_or_else(|| format!("missing stdlib output descriptor for {block}.{name}"))?;
        interface_outputs.insert(
            (*name).to_owned(),
            json!(if type_ref == "Any" {
                "graphblocks.ai/JsonValue@1"
            } else {
                type_ref
            }),
        );
    }
    let node_outputs = output_ports
        .iter()
        .map(|name| ((*name).to_owned(), json!(format!("$output.{name}"))))
        .collect::<Map<_, _>>();
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": format!("stdlib-{}", block.replace(['.', '@'], "-"))},
        "spec": {
            "interface": {
                "inputs": interface_inputs,
                "outputs": interface_outputs,
            },
            "nodes": {
                "subject": {
                    "block": block,
                    "inputs": node_inputs,
                    "config": config,
                    "outputs": node_outputs,
                }
            }
        }
    });
    let result_json = run_stdlib_graph_json(&graph.to_string(), &inputs.to_string())
        .map_err(|error| error.to_string())?;
    let result: Value = serde_json::from_str(&result_json).map_err(|error| error.to_string())?;
    if result["status"] != "succeeded" {
        return Err(format!("block {block} did not succeed: {result}"));
    }
    Ok(result["outputs"].clone())
}

#[test]
fn structured_generation_emits_schema_bound_items() -> Result<(), String> {
    let outputs = run_block(
        "model.structured_generate@1",
        json!({"diagnosis": {"items": [{"patch": "fix"}]}}),
        json!({"outputSchema": "company.hdl/PatchCandidateSet@1"}),
        &["response", "items", "schemaRef"],
    )?;

    assert_eq!(outputs["items"], json!([{"patch": "fix"}]));
    assert_eq!(outputs["schemaRef"], "company.hdl/PatchCandidateSet@1");
    Ok(())
}

#[test]
fn retrieval_adapters_execute_fuse_rank_and_build_context() -> Result<(), String> {
    let source_a = json!({
        "sourceId": "dense",
        "hits": [
            {"hitId": "a", "canonicalSource": "document-a", "rank": 1, "item": {"itemId": "document-a", "preview": ["rust graph runtime"]}},
            {"hitId": "b", "canonicalSource": "document-b", "rank": 2, "item": {"itemId": "document-b", "preview": ["unrelated"]}}
        ]
    });
    let source_b = json!({
        "sourceId": "keyword",
        "hits": [
            {"hitId": "a-copy", "canonicalSource": "document-a", "rank": 1, "item": {"itemId": "document-a", "preview": ["rust graph runtime"]}}
        ]
    });
    let retrieval = run_block(
        "retrieve.execute_plan@1",
        json!({"query": "rust graph", "sources": [source_a.clone(), source_b.clone()]}),
        json!({"minimumSuccessfulSources": 2}),
        &["result", "sources"],
    )?;
    assert_eq!(
        retrieval["result"]["successfulSources"],
        json!(["dense", "keyword"])
    );

    let fused = run_block(
        "retrieve.fuse@1",
        json!({"sources": retrieval["sources"]}),
        json!({"algorithm": "reciprocal_rank_fusion"}),
        &["hits"],
    )?;
    assert_eq!(fused["hits"].as_array().map(Vec::len), Some(2));
    assert_eq!(fused["hits"][0]["canonicalSource"], "document-a");

    let ranked = run_block(
        "rank.documents@1",
        json!({"query": "rust graph", "hits": fused["hits"]}),
        json!({}),
        &["hits"],
    )?;
    assert_eq!(ranked["hits"][0]["canonicalSource"], "document-a");
    assert_eq!(ranked["hits"][0]["rerankScore"], 2);

    let context = run_block(
        "context.build@1",
        json!({
            "history": [],
            "evidence": ranked["hits"],
            "currentMessage": {"text": "Explain the runtime"}
        }),
        json!({"maxTokens": 200, "reserveOutputTokens": 20}),
        &["pack"],
    )?;
    assert_eq!(context["pack"]["hits"].as_array().map(Vec::len), Some(2));
    assert_eq!(context["pack"]["tokenBudget"], 200);
    assert_eq!(
        context["pack"]["metadata"]["effective_context_token_budget"],
        180
    );
    Ok(())
}

#[test]
fn grounding_defaults_require_citations_and_abstain() -> Result<(), String> {
    let grounded = run_block(
        "answer.validate_grounding@1",
        json!({
            "response": {"text": "unsupported", "citations": []},
            "context": {"hits": [{"hitId": "support"}]}
        }),
        json!({}),
        &["result", "response"],
    )?;

    assert_eq!(grounded["result"]["policy"], "abstain");
    assert_eq!(grounded["result"]["abstained"], true);
    assert_eq!(
        grounded["result"]["issues"],
        json!(["grounding.citation_required"])
    );
    Ok(())
}

#[test]
fn check_suite_and_gate_accept_the_python_contract_vocabulary() -> Result<(), String> {
    let empty = run_block(
        "check.run_suite@1",
        json!({}),
        json!({"checks": []}),
        &["results", "passed", "hardGatePassed", "diagnostics"],
    )?;
    assert_eq!(empty["passed"], false);

    let configured = run_block(
        "check.run_suite@1",
        json!({}),
        json!({
            "checks": [
                {"checkId": "inline", "status": "inconclusive"},
                "unreached"
            ],
            "outcomes": {"unreached": "passed"},
            "stopOnFailure": true
        }),
        &["results", "passed", "hardGatePassed", "diagnostics"],
    )?;
    assert_eq!(configured["results"].as_array().map(Vec::len), Some(1));
    assert_eq!(configured["results"][0]["status"], "inconclusive");

    let outcomes = run_block(
        "check.run_suite@1",
        json!({}),
        json!({"checks": ["configured"], "outcomes": {"configured": "passed"}}),
        &["results", "passed", "hardGatePassed", "diagnostics"],
    )?;
    assert_eq!(outcomes["passed"], true);

    let gate = run_block(
        "gate.evaluate@1",
        json!({"checks": [
            {"checkId": "required", "status": "passed"},
            {"checkId": "ignored", "status": "failed"}
        ]}),
        json!({
            "requiredChecks": ["required"],
            "hardConstraints": ["ignored"]
        }),
        &["result", "passed"],
    )?;
    assert_eq!(gate["passed"], true);
    assert_eq!(gate["result"]["checkIds"], json!(["required"]));

    let informational = run_block(
        "gate.evaluate@1",
        json!({"checks": [
            {"checkId": "required", "status": "passed"},
            {"checkId": "ignored", "status": "inconclusive"}
        ]}),
        json!({"requiredChecks": ["required"]}),
        &["result", "passed"],
    )?;
    assert_eq!(informational["passed"], true);

    for status in ["inconclusive", "error", "timeout"] {
        let required_inconclusive = run_block(
            "gate.evaluate@1",
            json!({"checks": [{"checkId": "required", "status": status}]}),
            json!({"requiredChecks": ["required"]}),
            &["result", "passed"],
        )?;
        assert_eq!(required_inconclusive["passed"], false);
        assert_eq!(required_inconclusive["result"]["decision"], "inconclusive");
        assert_eq!(
            required_inconclusive["result"]["violatedConstraints"],
            json!([])
        );
    }

    for malformed in [
        json!({"checks": [{"status": "failed"}]}),
        json!({"checks": [true]}),
        json!({"checks": [
            {"checkId": "duplicate", "status": "passed"},
            {"checkId": "duplicate", "status": "failed"}
        ]}),
    ] {
        run_block(
            "gate.evaluate@1",
            malformed,
            json!({}),
            &["result", "passed"],
        )
        .expect_err("malformed or duplicate checks must fail closed");
    }
    run_block(
        "check.run_suite@1",
        json!({}),
        json!({"checks": [" "]}),
        &["results", "passed", "hardGatePassed", "diagnostics"],
    )
    .expect_err("blank check ids must fail closed");
    Ok(())
}

#[test]
fn retrieval_plan_emits_fused_result_and_requires_real_source_results() -> Result<(), String> {
    let missing = run_block(
        "retrieve.execute_plan@1",
        json!({"query": "rust", "sources": [{"sourceId": "missing"}]}),
        json!({"minimumSuccessfulSources": 1}),
        &["result", "sources"],
    )
    .expect_err("a source without result or hits must not count as successful");
    assert!(missing.contains("did not succeed"), "{missing}");

    let retrieval = run_block(
        "retrieve.execute_plan@1",
        json!({
            "request": {"queryText": "rust", "topK": 10},
            "sources": [
                {"sourceId": "ok", "result": {"hits": [
                    {"hitId": "a", "rank": 1, "item": {"itemId": "a"}},
                    {"hitId": "b", "rank": 2, "item": {"itemId": "b"}}
                ]}},
                {"sourceId": "missing"}
            ]
        }),
        json!({
            "minimumSuccessfulSources": 1,
            "topK": 1,
            "failureMode": "partial",
            "algorithm": "concatenate",
            "k": 7
        }),
        &["result", "sources"],
    )?;

    assert_eq!(retrieval["result"]["request"]["topK"], 1);
    assert_eq!(
        retrieval["result"]["hits"].as_array().map(Vec::len),
        Some(1)
    );
    assert_eq!(retrieval["result"]["totalCandidates"], 2);
    assert_eq!(retrieval["result"]["successfulSources"], json!(["ok"]));
    assert_eq!(
        retrieval["result"]["failedSources"]
            .as_array()
            .map(Vec::len),
        Some(1)
    );
    let expected_digest = canonical_hash(&json!({
        "query_text": "rust",
        "top_k": 1,
        "filters": {},
        "successful_sources": ["ok"],
        "failed_sources": [{"source_id": "missing", "error": "missing retrieval result"}],
        "fusion_strategy": "concatenate",
    }));
    assert_eq!(
        retrieval["result"]["retrievalId"],
        format!("federated:{expected_digest}")
    );
    assert_eq!(
        retrieval["result"]["metadata"]["failed_sources"][0]["source_id"],
        "missing"
    );
    Ok(())
}

#[test]
fn ranking_orders_before_truncation_and_emits_provenance_outputs() -> Result<(), String> {
    let ranked = run_block(
        "rank.documents@1",
        json!({
            "query": {"queryTerms": ["foo bar"]},
            "hits": [
                {"hitId": "later", "rank": 2, "item": {"itemId": "later", "preview": ["foo-bar"]}},
                {"hitId": "first", "rank": 1, "item": {"itemId": "first", "preview": ["foo bar"]}}
            ]
        }),
        json!({"inputLimit": 1}),
        &["hits"],
    )?;

    assert_eq!(ranked["hits"][0]["hitId"], "first");
    assert_eq!(ranked["hits"][0]["rerankScore"], 1);
    assert_eq!(ranked["hits"][0]["reranker"], "lexical");

    let unicode = run_block(
        "rank.documents@1",
        json!({
            "query": {"queryTerms": ["ärende"]},
            "hits": [
                {"hitId": "plain", "rank": 1, "item": {"itemId": "plain", "preview": ["other"]}},
                {"hitId": "match", "rank": 2, "item": {"itemId": "match", "preview": ["Ärende"]}}
            ]
        }),
        json!({}),
        &["hits"],
    )?;
    assert_eq!(unicode["hits"][0]["hitId"], "match");
    Ok(())
}

#[test]
fn context_build_applies_default_budget_dedupe_freshness_and_limits() -> Result<(), String> {
    let context = run_block(
        "context.build@1",
        json!({"evidence": [
            {
                "hitId": "selected", "rank": 2, "retriever": "dense",
                "item": {
                    "itemId": "item-a", "preview": ["current evidence"],
                    "metadata": {"document_id": "doc-a", "source_modified_at": "2026-07-17T00:00:00Z"}
                }
            },
            {
                "hitId": "stale", "rank": 1, "retriever": "dense",
                "item": {
                    "itemId": "item-stale", "preview": ["stale evidence"],
                    "metadata": {"document_id": "doc-stale", "source_modified_at": "2025-01-01T00:00:00Z"}
                }
            },
            {
                "hitId": "duplicate", "rank": 3, "retriever": "other",
                "item": {
                    "itemId": "item-a", "preview": ["duplicate evidence"],
                    "metadata": {"document_id": "doc-other", "source_modified_at": "2026-07-17T00:00:00Z"}
                }
            },
            {
                "hitId": "same-document", "rank": 4, "retriever": "other",
                "item": {
                    "itemId": "item-b", "preview": ["extra evidence"],
                    "metadata": {"document_id": "doc-a", "source_modified_at": "2026-07-17T00:00:00Z"}
                }
            }
        ]}),
        json!({
            "perDocumentMaxChunks": 1,
            "minimumSourceModifiedAt": "2026-01-01T00:00:00Z"
        }),
        &["pack"],
    )?;

    assert_eq!(context["pack"]["tokenBudget"], 4096);
    assert_eq!(context["pack"]["hits"].as_array().map(Vec::len), Some(1));
    assert_eq!(context["pack"]["hits"][0]["hitId"], "selected");
    assert_eq!(
        context["pack"]["metadata"]["drop_reasons"]["stale"],
        "freshness"
    );
    assert_eq!(
        context["pack"]["metadata"]["drop_reasons"]["duplicate"],
        "duplicate"
    );
    assert_eq!(
        context["pack"]["metadata"]["drop_reasons"]["same-document"],
        "per_document_max_chunks"
    );

    let highlight_sections = run_block(
        "context.build@1",
        json!({"evidence": [
            {
                "hitId": "first", "rank": 1,
                "item": {"itemId": "first", "preview": ["one"]},
                "highlights": [{"locator": null}, {"locator": {"elementId": "section-a"}}]
            },
            {
                "hitId": "second", "rank": 2,
                "item": {"itemId": "second", "preview": ["two"]},
                "highlights": [{"locator": {"elementId": "section-a"}}]
            }
        ]}),
        json!({"perSectionMaxChunks": 1}),
        &["pack"],
    )?;
    assert_eq!(
        highlight_sections["pack"]["hits"].as_array().map(Vec::len),
        Some(1)
    );
    assert_eq!(
        highlight_sections["pack"]["metadata"]["drop_reasons"]["second"],
        "per_section_max_chunks"
    );

    run_block(
        "context.build@1",
        json!({"evidence": []}),
        json!({"minimumSourceModifiedAt": "not-a-timestamp"}),
        &["pack"],
    )
    .expect_err("invalid freshness cutoffs must not silently drop all evidence");
    Ok(())
}

#[test]
fn retrieval_fusion_uses_locator_then_item_identity() -> Result<(), String> {
    let distinct_items = run_block(
        "retrieve.fuse@1",
        json!({"sources": [{"sourceId": "one", "hits": [
            {"hitId": "a", "rank": 1, "item": {"itemId": "a", "source": {"sourceId": "shared"}}},
            {"hitId": "b", "rank": 2, "item": {"itemId": "b", "source": {"sourceId": "shared"}}}
        ]}]}),
        json!({"algorithm": "concatenate"}),
        &["hits"],
    )?;
    assert_eq!(distinct_items["hits"].as_array().map(Vec::len), Some(2));

    let shared_locator = json!({"documentId": "doc", "chunkId": "chunk"});
    let equivalent_spans = run_block(
        "retrieve.fuse@1",
        json!({"sources": [{"sourceId": "one", "hits": [
            {"hitId": "a", "rank": 1, "item": {"itemId": "a", "source": {"locator": shared_locator}}},
            {"hitId": "b", "rank": 2, "item": {"itemId": "b", "source": {"locator": {"document_id": "doc", "chunk_id": "chunk"}}}}
        ]}]}),
        json!({"algorithm": "concatenate"}),
        &["hits"],
    )?;
    assert_eq!(equivalent_spans["hits"].as_array().map(Vec::len), Some(1));

    let later_highlight = run_block(
        "retrieve.fuse@1",
        json!({"sources": [{"sourceId": "one", "hits": [
            {
                "hitId": "a", "rank": 1, "item": {"itemId": "a"},
                "highlights": [{"locator": null}, {"locator": {"documentId": "doc", "chunkId": "chunk"}}]
            },
            {
                "hitId": "b", "rank": 2,
                "item": {"itemId": "b", "source": {"locator": {"document_id": "doc", "chunk_id": "chunk"}}}
            }
        ]}]}),
        json!({"algorithm": "concatenate"}),
        &["hits"],
    )?;
    assert_eq!(later_highlight["hits"].as_array().map(Vec::len), Some(1));
    Ok(())
}

#[test]
fn grounding_and_evaluation_fail_closed_without_evidence() -> Result<(), String> {
    let grounded = run_block(
        "answer.validate_grounding@1",
        json!({
            "response": {"text": "unsupported answer", "citations": []},
            "context": {"hits": []}
        }),
        json!({"requireCitation": true, "onInsufficientEvidence": "abstain"}),
        &["result", "response"],
    )?;
    assert_eq!(grounded["result"]["ok"], false);
    assert_eq!(grounded["result"]["abstained"], true);

    let checks = run_block(
        "check.run_suite@1",
        json!({
            "subject": {"checks": {"lint": true, "compile": false}}
        }),
        json!({"checks": ["lint", "compile"]}),
        &["results", "passed", "hardGatePassed", "diagnostics"],
    )?;
    assert_eq!(checks["passed"], false);
    assert_eq!(checks["results"][1]["status"], "failed");

    let gate = run_block(
        "gate.evaluate@1",
        json!({"checks": checks["results"]}),
        json!({"hardConstraints": ["lint", "compile"]}),
        &["result", "passed"],
    )?;
    assert_eq!(gate["passed"], false);
    assert_eq!(
        gate["result"]["violatedConstraints"],
        json!(["check:compile"])
    );
    Ok(())
}

#[test]
fn review_and_result_bundle_preserve_subject_and_evidence() -> Result<(), String> {
    let review = run_block(
        "review.request@1",
        json!({
            "subject": {"memo": "approved content"},
            "review": {
                "reviewId": "review-1",
                "decision": "accept",
                "reviewer": {"principalId": "reviewer-1"},
                "credentialRefs": ["attorney-license"]
            }
        }),
        json!({
            "scope": "substantive",
            "requiredCredential": "attorney-license",
            "invalidateOnSubjectChange": true
        }),
        &["request", "approved", "accepted"],
    )?;
    assert_eq!(review["approved"], true);
    assert_eq!(review["accepted"], true);
    assert_eq!(review["request"]["decision"], "accept");

    let bundle = run_block(
        "result.bundle@1",
        json!({
            "outputs": [{"memo": "approved content"}],
            "evidence": [{"sourceId": "authority-1"}],
            "checks": [{"checkId": "citations", "status": "passed"}],
            "reviews": [review["request"]]
        }),
        json!({"runId": "run-1", "releaseId": "release-1"}),
        &["result"],
    )?;
    assert_eq!(bundle["result"]["runId"], "run-1");
    assert_eq!(
        bundle["result"]["content"]["reviews"][0]["decision"],
        "accept"
    );
    assert!(bundle["result"]["contentDigest"].as_str().is_some());
    Ok(())
}
