use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::{Map, Value, json};

fn single_node_graph(
    name: &str,
    block: &str,
    interface_inputs: Value,
    interface_outputs: Value,
    node_inputs: Value,
    node_outputs: Value,
    config: Value,
) -> Value {
    json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": name},
        "spec": {
            "interface": {
                "inputs": interface_inputs,
                "outputs": interface_outputs,
            },
            "nodes": {
                "subject": {
                    "block": block,
                    "inputs": node_inputs,
                    "outputs": node_outputs,
                    "config": config,
                }
            }
        }
    })
}

fn run_succeeded(graph: &Value, inputs: &Value) -> Result<Value, String> {
    let result_json = run_stdlib_graph_json(&graph.to_string(), &inputs.to_string())
        .map_err(|error| error.to_string())?;
    let result: Value = serde_json::from_str(&result_json).map_err(|error| error.to_string())?;
    if result["status"] != "succeeded" {
        return Err(format!("graph did not succeed: {result}"));
    }
    Ok(result["outputs"].clone())
}

#[test]
fn retrieve_fuse_emits_its_advertised_metadata_output() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "retrieve-fuse-metadata"},
        "spec": {
            "interface": {
                "inputs": {"sources": "graphblocks.ai/RetrievalSources@1"},
                "outputs": {"observation": "graphblocks.ai/Any@1"},
            },
            "nodes": {
                "fuse": {
                    "block": "retrieve.fuse@1",
                    "inputs": {"sources": "$input.sources"},
                },
                "observe": {
                    "block": "result.bundle@1",
                    "inputs": {
                        "outputs": "fuse.hits",
                        "gate": "fuse.metadata",
                    },
                    "outputs": {"result": "$output.observation"},
                }
            }
        }
    });
    let outputs = run_succeeded(
        &graph,
        &json!({
            "sources": [{
                "sourceId": "primary",
                "hits": [{
                    "hitId": "hit-1",
                    "canonicalSource": "document-1",
                    "rank": 1,
                    "text": "relevant",
                }],
            }]
        }),
    )?;

    assert_eq!(
        outputs.pointer("/observation/content/gate"),
        Some(&json!({
            "algorithm": "reciprocal_rank_fusion",
            "sourceCount": 1,
        }))
    );
    Ok(())
}

#[test]
fn retrieve_execute_plan_accepts_the_request_input_alias() -> Result<(), String> {
    let graph = single_node_graph(
        "retrieve-request-alias",
        "retrieve.execute_plan@1",
        json!({
            "request": "graphblocks.ai/SearchRequest@1",
            "sources": "graphblocks.ai/FederatedSources@1",
        }),
        json!({"result": "graphblocks.ai/RetrievalResult@1"}),
        json!({
            "request": "$input.request",
            "sources": "$input.sources",
        }),
        json!({"result": "$output.result"}),
        json!({"minimumSuccessfulSources": 1}),
    );
    let request = json!({"queryText": "graph runtime", "topK": 4});
    let outputs = run_succeeded(
        &graph,
        &json!({
            "request": request,
            "sources": [{
                "sourceId": "primary",
                "hits": [{"hitId": "hit-1", "text": "graph runtime"}],
            }],
        }),
    )?;

    assert_eq!(outputs.pointer("/result/query"), Some(&request));
    Ok(())
}

#[test]
fn rank_documents_uses_structured_search_request_terms() -> Result<(), String> {
    let graph = single_node_graph(
        "rank-structured-request",
        "rank.documents@1",
        json!({
            "query": "graphblocks.ai/SearchRequest@1",
            "hits": "graphblocks.ai/SearchHits@1",
        }),
        json!({"hits": "graphblocks.ai/SearchHits@1"}),
        json!({
            "query": "$input.query",
            "hits": "$input.hits",
        }),
        json!({"hits": "$output.hits"}),
        json!({}),
    );
    let outputs = run_succeeded(
        &graph,
        &json!({
            "query": {
                "queryTerms": ["needle"],
                "queryText": "distractor",
            },
            "hits": [
                {
                    "hitId": "distractor-hit",
                    "canonicalSource": "distractor-document",
                    "item": {"preview": "distractor querytext"},
                },
                {
                    "hitId": "needle-hit",
                    "canonicalSource": "needle-document",
                    "item": {"preview": "needle"},
                },
            ],
        }),
    )?;

    assert_eq!(outputs.pointer("/hits/0/hitId"), Some(&json!("needle-hit")));
    Ok(())
}

#[test]
fn context_build_accepts_the_hits_input_alias() -> Result<(), String> {
    let graph = single_node_graph(
        "context-hits-alias",
        "context.build@1",
        json!({"hits": "graphblocks.ai/SearchHits@1"}),
        json!({"pack": "graphblocks.ai/ContextPack@1"}),
        json!({"hits": "$input.hits"}),
        json!({"pack": "$output.pack"}),
        json!({"maxTokens": 100}),
    );
    let outputs = run_succeeded(
        &graph,
        &json!({
            "hits": [{
                "hitId": "hit-1",
                "canonicalSource": "document-1",
                "text": "grounded context",
            }]
        }),
    )?;

    assert_eq!(outputs.pointer("/pack/hits/0/hitId"), Some(&json!("hit-1")));
    Ok(())
}

#[test]
fn grounding_validation_accepts_the_answer_input_alias() -> Result<(), String> {
    let graph = single_node_graph(
        "grounding-answer-alias",
        "answer.validate_grounding@1",
        json!({
            "answer": "graphblocks.ai/Answer@1",
            "context": "graphblocks.ai/ContextPack@1",
        }),
        json!({"candidate": "graphblocks.ai/Answer@1"}),
        json!({
            "answer": "$input.answer",
            "context": "$input.context",
        }),
        json!({"candidate": "$output.candidate"}),
        json!({"requireCitation": true}),
    );
    let answer = json!({"text": "grounded answer", "citations": ["source-1"]});
    let outputs = run_succeeded(
        &graph,
        &json!({
            "answer": answer,
            "context": {"hits": [{"hitId": "source-1"}]},
        }),
    )?;

    assert_eq!(outputs["candidate"], answer);
    Ok(())
}

#[test]
fn gate_evaluate_uses_subject_and_metric_constraints() -> Result<(), String> {
    let graph = single_node_graph(
        "gate-subject-metrics",
        "gate.evaluate@1",
        json!({
            "checks": "graphblocks.ai/Any@1",
            "metrics": "graphblocks.ai/Any@1",
            "subject": "graphblocks.ai/Any@1",
        }),
        json!({
            "result": "graphblocks.ai/Any@1",
            "decision": "graphblocks.ai/String@1",
            "violations": "graphblocks.ai/Any@1",
        }),
        json!({
            "checks": "$input.checks",
            "metrics": "$input.metrics",
            "subject": "$input.subject",
        }),
        json!({
            "result": "$output.result",
            "decision": "$output.decision",
            "violations": "$output.violations",
        }),
        json!({
            "hardConstraints": ["lint"],
            "constraints": [{
                "metric": "coverage",
                "operator": "at_least",
                "threshold": 90,
            }],
        }),
    );
    let outputs = run_succeeded(
        &graph,
        &json!({
            "subject": {"resourceId": "release-1", "digest": "sha256:release-1"},
            "checks": [{"checkId": "lint", "status": "passed"}],
            "metrics": [{"name": "coverage", "value": 80}],
        }),
    )?;

    assert_eq!(outputs["decision"], "fail");
    assert_eq!(outputs["violations"], json!(["metric:coverage"]));
    assert_eq!(
        outputs
            .pointer("/result/subject/resourceId")
            .or_else(|| outputs.pointer("/result/subject/resource_id")),
        Some(&json!("release-1"))
    );
    Ok(())
}

fn assert_review_principal_alias(alias: &str) -> Result<(), String> {
    let mut interface_inputs =
        Map::from_iter([("subject".to_owned(), json!("graphblocks.ai/Any@1"))]);
    interface_inputs.insert(alias.to_owned(), json!("graphblocks.ai/Principal@1"));
    let mut review_inputs = Map::from_iter([("subject".to_owned(), json!("$input.subject"))]);
    review_inputs.insert(alias.to_owned(), json!(format!("$input.{alias}")));
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": format!("review-{alias}-alias")},
        "spec": {
            "interface": {
                "inputs": interface_inputs,
                "outputs": {"observation": "graphblocks.ai/Any@1"},
            },
            "nodes": {
                "review": {
                    "block": "review.request@1",
                    "inputs": review_inputs,
                    "config": {"scope": "design_intent"},
                },
                "observe": {
                    "block": "result.bundle@1",
                    "inputs": {
                        "outputs": "review.request",
                        "gate": "review.requestDigest",
                    },
                    "outputs": {"result": "$output.observation"},
                }
            }
        }
    });
    let principal = json!({
        "principalId": "author-1",
        "tenantId": "tenant-1",
        "roles": ["author"],
    });
    let mut runtime_inputs = Map::from_iter([("subject".to_owned(), json!({"memo": "review me"}))]);
    runtime_inputs.insert(alias.to_owned(), principal.clone());
    let outputs = run_succeeded(&graph, &Value::Object(runtime_inputs))?;

    assert_eq!(
        outputs.pointer("/observation/content/outputs/requestedBy"),
        Some(&principal)
    );
    assert!(
        outputs
            .pointer("/observation/content/gate")
            .and_then(Value::as_str)
            .is_some_and(|digest| digest.starts_with("sha256:")),
        "review.request@1 did not emit requestDigest"
    );
    Ok(())
}

#[test]
fn review_request_accepts_requested_by_camel_case() -> Result<(), String> {
    assert_review_principal_alias("requestedBy")
}

#[test]
fn review_request_accepts_requested_by_snake_case() -> Result<(), String> {
    assert_review_principal_alias("requested_by")
}

#[test]
fn result_bundle_preserves_all_advertised_evidence_inputs() -> Result<(), String> {
    let graph = single_node_graph(
        "result-bundle-evidence-inputs",
        "result.bundle@1",
        json!({
            "inputs": "graphblocks.ai/Any@1",
            "outputs": "graphblocks.ai/Any@1",
            "diagnostics": "graphblocks.ai/Any@1",
            "usage": "graphblocks.ai/Any@1",
            "usageRecords": "graphblocks.ai/Any@1",
            "policyDecisionRefs": "graphblocks.ai/Any@1",
        }),
        json!({"bundle": "graphblocks.ai/Any@1"}),
        json!({
            "inputs": "$input.inputs",
            "outputs": "$input.outputs",
            "diagnostics": "$input.diagnostics",
            "usage": "$input.usage",
            "usageRecords": "$input.usageRecords",
            "policyDecisionRefs": "$input.policyDecisionRefs",
        }),
        json!({"result": "$output.bundle"}),
        json!({"runId": "run-1", "releaseId": "release-1"}),
    );
    let inputs = json!({
        "inputs": [{"resourceId": "input-1", "digest": "sha256:input-1"}],
        "outputs": [{"valueId": "output-1", "schemaId": "company/Result@1"}],
        "diagnostics": [{"code": "D001", "message": "preserve me"}],
        "usage": ["usage-primary"],
        "usageRecords": ["usage-alias"],
        "policyDecisionRefs": ["policy-decision-1"],
    });
    let outputs = run_succeeded(&graph, &inputs)?;
    let content = outputs
        .pointer("/bundle/content")
        .ok_or_else(|| "result bundle is missing content".to_owned())?;

    for name in [
        "inputs",
        "diagnostics",
        "usage",
        "usageRecords",
        "policyDecisionRefs",
    ] {
        assert_eq!(
            content.get(name),
            inputs.get(name),
            "result.bundle@1 dropped its {name} input"
        );
    }
    Ok(())
}
