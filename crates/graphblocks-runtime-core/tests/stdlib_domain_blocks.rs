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
            {"hitId": "a", "canonicalSource": "document-a", "rank": 1, "item": {"preview": ["rust graph runtime"]}},
            {"hitId": "b", "canonicalSource": "document-b", "rank": 2, "item": {"preview": ["unrelated"]}}
        ]
    });
    let source_b = json!({
        "sourceId": "keyword",
        "hits": [
            {"hitId": "a-copy", "canonicalSource": "document-a", "rank": 1, "item": {"preview": ["rust graph runtime"]}}
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
    assert_eq!(context["pack"]["tokenBudget"], 180);
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
