use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_json;
use serde_json::{Value, json};

#[test]
fn rust_stdlib_runtime_executes_prompt_render_graph() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-prompt-render"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Text@1"},
                "outputs": {"prompt": "graphblocks.ai/Text@1"}
            },
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Test {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({"message": {"text": "ok"}}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"], json!({"prompt": "Test ok"}));
    Ok(())
}

#[test]
fn rust_stdlib_runtime_preserves_tool_implementation_mappings() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-tool-mappings"},
        "spec": {
            "interface": {
                "outputs": {"tools": "graphblocks.ai/ResolvedTools@1"}
            },
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "block.search",
                                "description": "Search through a block implementation.",
                                "inputSchema": "schemas/SearchRequest@1"
                            },
                            {
                                "name": "graph.search",
                                "description": "Search through a graph implementation.",
                                "inputSchema": "schemas/SearchRequest@1"
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-block-search",
                                "toolName": "block.search",
                                "implementation": {
                                    "kind": "block",
                                    "block": "knowledge.search@1",
                                    "inputMapping": {"query": "$args.query"},
                                    "outputMapping": {"items": "$result.items"}
                                },
                                "effects": ["external_read"],
                                "approval": "never"
                            },
                            {
                                "bindingId": "binding-graph-search",
                                "toolName": "graph.search",
                                "implementation": {
                                    "kind": "graph",
                                    "graph": "graphs/knowledge-search",
                                    "input_mapping": {"query": "$args.query"},
                                    "output_mapping": {"items": "$result.items"}
                                },
                                "effects": ["external_read"],
                                "approval": "never"
                            }
                        ],
                        "scope": {
                            "principalTools": ["block.search", "graph.search"]
                        }
                    },
                    "outputs": {"tools": "$output.tools"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;
    let tools = result["outputs"]["tools"]
        .as_array()
        .ok_or_else(|| "resolved tools output must be an array".to_owned())?;

    let block = resolved_tool_by_name(tools, "block.search")?;
    assert_eq!(
        block.pointer("/binding/implementation/input_mapping"),
        Some(&json!({"query": "$args.query"})),
    );
    assert_eq!(
        block.pointer("/binding/implementation/output_mapping"),
        Some(&json!({"items": "$result.items"})),
    );

    let graph = resolved_tool_by_name(tools, "graph.search")?;
    assert_eq!(
        graph.pointer("/binding/implementation/input_mapping"),
        Some(&json!({"query": "$args.query"})),
    );
    assert_eq!(
        graph.pointer("/binding/implementation/output_mapping"),
        Some(&json!({"items": "$result.items"})),
    );
    Ok(())
}

#[test]
fn rust_stdlib_runtime_rejects_invalid_tool_tags_and_scope_entries() -> Result<(), String> {
    let cases = [
        (
            "non-string tag",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1",
                        "tags": ["search", 1]
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {"kind": "block", "block": "knowledge.search@1"},
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search"]}
            }),
            "tools.resolve@1 config.definitions[0].tags[1] must be a string",
        ),
        (
            "blank tag",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1",
                        "tags": ["search", " "]
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {"kind": "block", "block": "knowledge.search@1"},
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search"]}
            }),
            "tools.resolve@1 config.definitions[0].tags[1] must not be empty",
        ),
        (
            "blank input mapping key",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1"
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {
                            "kind": "block",
                            "block": "knowledge.search@1",
                            "inputMapping": {" ": "$args.query"}
                        },
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search"]}
            }),
            "tools.resolve@1 implementation.inputMapping keys must not be empty",
        ),
        (
            "blank output mapping value",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1"
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {
                            "kind": "graph",
                            "graph": "graphs/knowledge-search",
                            "outputMapping": {"items": " "}
                        },
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search"]}
            }),
            "tools.resolve@1 implementation.outputMapping.items must not be empty",
        ),
        (
            "non-string scope entry",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1"
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {"kind": "block", "block": "knowledge.search@1"},
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search", 1]}
            }),
            "tools.resolve@1 config.scope.principalTools[1] must be a string",
        ),
        (
            "blank scope entry",
            json!({
                "effectivePolicySnapshotId": "policy-snapshot-1",
                "definitions": [
                    {
                        "name": "knowledge.search",
                        "description": "Search support documentation.",
                        "inputSchema": "schemas/SearchRequest@1"
                    }
                ],
                "bindings": [
                    {
                        "bindingId": "binding-search",
                        "toolName": "knowledge.search",
                        "implementation": {"kind": "block", "block": "knowledge.search@1"},
                        "effects": ["external_read"],
                        "approval": "never"
                    }
                ],
                "scope": {"principalTools": ["knowledge.search", " "]}
            }),
            "tools.resolve@1 config.scope.principalTools[1] must not be empty",
        ),
    ];

    for (case_name, config, expected_error) in cases {
        let graph = json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "runtime-invalid-tool-resolution-config"},
            "spec": {
                "nodes": {
                    "resolve": {
                        "block": "tools.resolve@1",
                        "config": config,
                        "outputs": {"tools": "$output.tools"}
                    }
                }
            }
        });
        let result = run_graph(&graph, &json!({}))?;
        let node_error = result["journal"]
            .as_array()
            .and_then(|journal| {
                journal
                    .iter()
                    .find(|record| record["kind"].as_str() == Some("node_failed"))
            })
            .and_then(|record| record.pointer("/payload/message"))
            .and_then(Value::as_str)
            .ok_or_else(|| format!("{case_name}: missing node failure error"))?;

        assert_eq!(
            result["status"].as_str(),
            Some("failed"),
            "{case_name}: status mismatch",
        );
        assert!(
            node_error.contains(expected_error),
            "{case_name}: expected {expected_error:?} in {node_error:?}",
        );
    }
    Ok(())
}

#[test]
fn rust_stdlib_agent_run_surfaces_output_policy_profile_ref() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-agent-output-policy-profile"},
        "spec": {
            "interface": {
                "inputs": {
                    "messages": "graphblocks.ai/Messages@1",
                    "tools": "graphblocks.ai/ResolvedTools@1"
                },
                "outputs": {"candidate": "graphblocks.ai/TurnCandidate@1"}
            },
            "nodes": {
                "agent": {
                    "block": "agent.run@1",
                    "config": {
                        "response": "Hello from the agent.",
                        "outputPolicy": {"profileRef": "assistant-output-standard"}
                    },
                    "inputs": {
                        "messages": "$input.messages",
                        "tools": "$input.tools"
                    },
                    "outputs": {"candidate": "$output.candidate"}
                }
            }
        }
    });
    let result = run_graph(
        &graph,
        &json!({
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": []
        }),
    )?;

    assert_eq!(
        result["outputs"]["candidate"]["outputPolicyProfileRef"],
        json!("assistant-output-standard")
    );
    Ok(())
}

#[test]
fn rust_stdlib_agent_run_rejects_unresolved_tool_entries() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-agent-rejects-unresolved-tool"},
        "spec": {
            "interface": {
                "inputs": {
                    "messages": "graphblocks.ai/Messages@1",
                    "tools": "graphblocks.ai/ResolvedTools@1"
                },
                "outputs": {"candidate": "graphblocks.ai/TurnCandidate@1"}
            },
            "nodes": {
                "agent": {
                    "block": "agent.run@1",
                    "config": {"response": "should not run"},
                    "inputs": {
                        "messages": "$input.messages",
                        "tools": "$input.tools"
                    },
                    "outputs": {"candidate": "$output.candidate"}
                }
            }
        }
    });
    let result = run_graph(
        &graph,
        &json!({
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"definition": {"name": "knowledge.search"}}]
        }),
    )?;

    assert_eq!(result["status"], "failed");
    let node_error = result["journal"]
        .as_array()
        .and_then(|journal| {
            journal
                .iter()
                .find(|record| record["kind"].as_str() == Some("node_failed"))
        })
        .and_then(|record| record.pointer("/payload/message"))
        .and_then(Value::as_str)
        .ok_or_else(|| "missing agent node failure".to_owned())?;
    assert!(
        node_error.contains("field resolved_tool_id must be a string"),
        "unexpected node error: {node_error:?}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_blocks_start_and_await_callback_operation() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-await"},
        "spec": {
            "interface": {
                "inputs": {"changeset": "graphblocks.ai/Changeset@1"},
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "expiresAtUnixMs": 1_800,
                        "timeoutMs": 800,
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"subject": "$input.changeset"},
                    "outputs": {"operation": "waitCI.operation"}
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "checkpoint": true,
                        "onTimeout": "fail",
                        "timeout": "800ms",
                        "idempotencyKey": "idem-op-ci-1",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true,
                        "callback": {
                            "required": true,
                            "schema": "schemas/CICallback@1"
                        }
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({"changeset": {"id": "changeset-1"}}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["wait"]["state"], "waiting_callback");
    assert_eq!(
        result["outputs"]["wait"]["operation"]["operation_id"],
        "op-ci-1"
    );
    assert_eq!(result["outputs"]["wait"]["operation"]["kind"], "ci_job");
    assert_eq!(result["outputs"]["wait"]["checkpoint"], true);
    assert_eq!(result["outputs"]["wait"]["onTimeout"], "fail");
    assert_eq!(result["outputs"]["wait"]["timeoutMs"], 800);
    Ok(())
}

#[test]
fn rust_stdlib_async_await_callback_accepts_explicit_infinite_wait_policy() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-await-infinite-wait"},
        "spec": {
            "interface": {
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "infiniteWaitPolicy": "operator_review_required",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "waitCI.operation"}
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "checkpoint": true,
                        "onTimeout": "fail",
                        "infiniteWaitPolicy": "operator_review_required",
                        "idempotencyKey": "idem-op-ci-1",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true,
                        "callback": {
                            "required": true,
                            "schema": "schemas/CICallback@1"
                        }
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["wait"]["state"], "waiting_callback");
    assert_eq!(
        result["outputs"]["wait"]["infiniteWaitPolicy"],
        "operator_review_required"
    );
    assert!(result["outputs"]["wait"].get("timeoutMs").is_none());
    Ok(())
}

#[test]
fn rust_stdlib_async_await_callback_rejects_invalid_timeout_duration() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-await-invalid-timeout"},
        "spec": {
            "interface": {
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeout": "30m",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "waitCI.operation"}
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "soon",
                        "idempotencyKey": "idem-op-ci-1",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true,
                        "callback": {
                            "required": true,
                            "schema": "schemas/CICallback@1"
                        }
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"}
                }
            }
        }
    });
    let error = run_graph(&graph, &json!({}))
        .expect_err("invalid await timeout should fail compiler diagnostics");

    assert!(
        error.contains("GB6001"),
        "unexpected compiler error: {error:?}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_await_callback_rejects_unknown_on_timeout_policy() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-await-invalid-on-timeout"},
        "spec": {
            "interface": {
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeout": "30m",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "waitCI.operation"}
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "timeout": "30m",
                        "onTimeout": "continue_anyway",
                        "idempotencyKey": "idem-op-ci-1",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true,
                        "callback": {
                            "required": true,
                            "schema": "schemas/CICallback@1"
                        }
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"}
                }
            }
        }
    });
    let error = run_graph(&graph, &json!({}))
        .expect_err("unknown await onTimeout policy should fail compiler diagnostics");

    assert!(
        error.contains("InvalidAsyncOperation"),
        "unexpected compiler error: {error:?}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_await_callback_rejects_non_boolean_checkpoint() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-await-invalid-checkpoint"},
        "spec": {
            "interface": {
                "outputs": {"wait": "graphblocks.ai/AsyncWait@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_100,
                        "timeout": "30m",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "waitCI.operation"}
                },
                "waitCI": {
                    "block": "async.await_callback@1",
                    "config": {
                        "checkpoint": "yes",
                        "onTimeout": "fail",
                        "timeout": "30m",
                        "idempotencyKey": "idem-op-ci-1",
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startCI.operation"},
                    "outputs": {"wait": "$output.wait"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.await_callback.invalid_config")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_accepts_relative_timeout_duration() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-timeout-duration"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeout": "30m",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["operation"]["state"], "waiting_callback");
    assert_eq!(
        result["outputs"]["operation"]["expires_at_unix_ms"],
        1_801_000
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_accepts_explicit_infinite_wait_policy() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-infinite-wait"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "infiniteWaitPolicy": "provider_has_no_timeout",
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["operation"]["state"], "waiting_callback");
    assert_eq!(
        result["outputs"]["operation"]["infinite_wait_policy"],
        "provider_has_no_timeout"
    );
    assert!(result["outputs"]["operation"]["expires_at_unix_ms"].is_null());
    Ok(())
}

#[test]
fn rust_stdlib_async_blocks_poll_complete_cancel_and_expire_operations() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-terminal-blocks"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1",
                    "completed": "graphblocks.ai/AsyncOperationResult@1",
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1",
                    "expired": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "intervalMs": 30_000,
                        "maxIntervalMs": 300_000,
                        "timeoutMs": 7_200_000,
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                },
                "startComplete": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-complete", "node-complete"),
                    "outputs": {"operation": "complete.operation"}
                },
                "complete": {
                    "block": "async.complete_operation@1",
                    "config": {
                        "completedAtUnixMs": 1_900,
                        "diagnostics": [{"severity": "info", "message": "checks complete"}],
                        "metrics": [{"name": "duration_ms", "value": 840}],
                        "checks": [{"name": "unit", "status": "passed"}],
                        "usage": [{"kind": "ci_minutes", "amount": 2}]
                    },
                    "inputs": {
                        "operation": "startComplete.operation",
                        "output": "$input.payload"
                    },
                    "outputs": {"result": "$output.completed"}
                },
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {
                        "cancelledAtUnixMs": 1_900,
                        "externalEffects": [
                            {
                                "effectId": "effect-ticket-1",
                                "target": "ticket-system",
                                "operation": "ticket.create",
                                "outcome": "committed",
                                "idempotencyKey": "idem-ticket-1",
                                "providerEffectId": "ticket-123"
                            }
                        ]
                    },
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                },
                "startExpire": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-expire", "node-expire"),
                    "outputs": {"operation": "expire.operation"}
                },
                "expire": {
                    "block": "async.expire_operation@1",
                    "config": {"expiredAtUnixMs": 1_900},
                    "inputs": {"operation": "startExpire.operation"},
                    "outputs": {"result": "$output.expired"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({"payload": {"status": "completed"}}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["poll"]["state"], "polling");
    assert_eq!(
        result["outputs"]["poll"]["operation"]["operation_id"],
        "op-poll"
    );
    assert_eq!(result["outputs"]["poll"]["intervalMs"], 30_000);
    assert_eq!(
        result["outputs"]["completed"]["operation_id"],
        "op-complete"
    );
    assert_eq!(result["outputs"]["completed"]["status"], "completed");
    assert_eq!(
        result["outputs"]["completed"]["completed_at_unix_ms"],
        1_900
    );
    assert_eq!(
        result["outputs"]["completed"]["diagnostics"],
        json!([{"severity": "info", "message": "checks complete"}])
    );
    assert_eq!(
        result["outputs"]["completed"]["metrics"],
        json!([{"name": "duration_ms", "value": 840}])
    );
    assert_eq!(
        result["outputs"]["completed"]["checks"],
        json!([{"name": "unit", "status": "passed"}])
    );
    assert_eq!(
        result["outputs"]["completed"]["usage"],
        json!([{"kind": "ci_minutes", "amount": 2}])
    );
    assert_eq!(
        result["outputs"]["completed"]["output"],
        json!({"status": "completed"})
    );
    assert_eq!(result["outputs"]["cancelled"]["operation_id"], "op-cancel");
    assert_eq!(result["outputs"]["cancelled"]["status"], "cancelled");
    assert_eq!(
        result["outputs"]["cancelled"]["external_effects"],
        json!([
            {
                "effect_id": "effect-ticket-1",
                "target": "ticket-system",
                "operation": "ticket.create",
                "outcome": "committed",
                "idempotency_key": "idem-ticket-1",
                "provider_effect_id": "ticket-123"
            }
        ])
    );
    assert_eq!(result["outputs"]["expired"]["operation_id"], "op-expire");
    assert_eq!(result["outputs"]["expired"]["status"], "expired");
    Ok(())
}

#[test]
fn rust_stdlib_async_terminal_blocks_reject_invalid_projection_entries() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "rust-stdlib-async-invalid-result-projection"},
        "spec": {
            "interface": {
                "outputs": {
                    "completed": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startComplete": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-complete", "node-complete"),
                    "outputs": {"operation": "complete.operation"}
                },
                "complete": {
                    "block": "async.complete_operation@1",
                    "config": {
                        "completedAtUnixMs": 1_900,
                        "diagnostics": ["not-a-diagnostic-object"]
                    },
                    "inputs": {"operation": "startComplete.operation"},
                    "outputs": {"result": "$output.completed"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|journal| {
            journal.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert_eq!(
        node_failed.pointer("/payload/code").and_then(Value::as_str),
        Some("async.complete_operation.invalid_config")
    );
    let message = node_failed
        .pointer("/payload/message")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing node_failed message".to_string())?;
    assert!(
        message.contains("config.diagnostics[0] must be an object"),
        "unexpected message: {message}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_runtime_matches_shared_runtime_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/runtime/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "runtime TCK root must be an array".to_owned())?;

    for case in cases {
        let case_name = case
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| "runtime TCK case missing name".to_owned())?;
        let document = case
            .get("document")
            .ok_or_else(|| format!("runtime TCK case {case_name} missing document"))?;
        let expected = case
            .get("expected")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("runtime TCK case {case_name} missing expected object"))?;
        let inputs = case.get("inputs").cloned().unwrap_or_else(|| json!({}));
        let result = run_graph(document, &inputs)?;
        let terminal_kind = result
            .get("journal")
            .and_then(Value::as_array)
            .and_then(|journal| {
                journal
                    .iter()
                    .rev()
                    .find(|record| record.get("terminal").and_then(Value::as_bool) == Some(true))
            })
            .and_then(|record| record.get("kind"))
            .and_then(Value::as_str);

        assert_eq!(
            result.get("status").and_then(Value::as_str),
            expected.get("status").and_then(Value::as_str),
            "runtime TCK case {case_name} status mismatch",
        );
        assert_eq!(
            terminal_kind,
            expected.get("terminal_kind").and_then(Value::as_str),
            "runtime TCK case {case_name} terminal kind mismatch",
        );
        assert_eq!(
            result.get("outputs"),
            expected.get("outputs"),
            "runtime TCK case {case_name} outputs mismatch",
        );
    }

    Ok(())
}

#[test]
fn rust_stdlib_async_poll_operation_requires_timeout() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-poll-timeout-required"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {"intervalMs": 30_000, "maxIntervalMs": 300_000},
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                }
            }
        }
    });
    let error = run_graph(&graph, &json!({}))
        .expect_err("unbounded async poll should fail compiler diagnostics");
    assert!(
        error.contains("GB6001"),
        "unexpected compiler error: {error:?}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_poll_operation_accepts_explicit_infinite_wait_policy() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-poll-infinite-wait"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "30s",
                        "maxInterval": "5m",
                        "infiniteWaitPolicy": "provider_has_no_timeout",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["poll"]["state"], "polling");
    assert_eq!(
        result["outputs"]["poll"]["infiniteWaitPolicy"],
        "provider_has_no_timeout"
    );
    assert!(result["outputs"]["poll"].get("timeoutMs").is_none());
    Ok(())
}

#[test]
fn rust_stdlib_async_poll_operation_accepts_duration_strings() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-poll-duration-strings"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "30s",
                        "maxInterval": "5m",
                        "timeout": "2h",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["outputs"]["poll"]["intervalMs"], 30_000);
    assert_eq!(result["outputs"]["poll"]["maxIntervalMs"], 300_000);
    assert_eq!(result["outputs"]["poll"]["timeoutMs"], 7_200_000);
    Ok(())
}

#[test]
fn rust_stdlib_async_poll_operation_rejects_max_interval_below_interval() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-poll-invalid-max-interval"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "5m",
                        "maxInterval": "30s",
                        "timeout": "2h",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                }
            }
        }
    });
    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.poll_operation.invalid_config")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_poll_operation_rejects_oversized_string_duration() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-poll-oversized-duration"},
        "spec": {
            "interface": {
                "outputs": {
                    "poll": "graphblocks.ai/AsyncPoll@1"
                }
            },
            "nodes": {
                "startPoll": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-poll", "node-poll"),
                    "outputs": {"operation": "poll.operation"}
                },
                "poll": {
                    "block": "async.poll_operation@1",
                    "config": {
                        "interval": "30s",
                        "timeout": "18446744073709551616ms",
                        "idempotencyKey": "idem-op-poll",
                        "callback": {"schema": "schemas/PollResult@1"},
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "inputs": {"operation": "startPoll.operation"},
                    "outputs": {"poll": "$output.poll"}
                }
            }
        }
    });
    let error = run_graph(&graph, &json!({}))
        .expect_err("oversized poll duration should fail compiler diagnostics");

    assert!(
        error.contains("GB6001"),
        "unexpected compiler error: {error:?}",
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_cancel_operation_rejects_invalid_terminal_timestamp() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-cancel-invalid-terminal-timestamp"},
        "spec": {
            "interface": {
                "outputs": {
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {"cancelledAtUnixMs": 1_000},
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;
    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.cancel_operation.invalid_config")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_cancel_operation_rejects_non_object_config() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-cancel-non-object-config"},
        "spec": {
            "interface": {
                "outputs": {
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": "invalid",
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;
    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("async.cancel_operation@1 config must be an object")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_cancel_operation_rejects_terminal_after_expiration() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-cancel-terminal-after-expiration"},
        "spec": {
            "interface": {
                "outputs": {
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {"cancelledAtUnixMs": 2_001},
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;
    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("terminal timestamp must not exceed expires_at_unix_ms")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_cancel_operation_rejects_malformed_terminal_timestamp() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-cancel-malformed-terminal-timestamp"},
        "spec": {
            "interface": {
                "outputs": {
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {"cancelledAtUnixMs": "soon"},
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;
    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.cancel_operation.invalid_config")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_expire_operation_rejects_invalid_terminal_timestamp() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-expire-invalid-terminal-timestamp"},
        "spec": {
            "interface": {
                "outputs": {
                    "expired": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startExpire": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-expire", "node-expire"),
                    "outputs": {"operation": "expire.operation"}
                },
                "expire": {
                    "block": "async.expire_operation@1",
                    "config": {"expiredAtUnixMs": 0},
                    "inputs": {"operation": "startExpire.operation"},
                    "outputs": {"result": "$output.expired"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;
    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.expire_operation.invalid_config")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_terminal_effects_reject_provider_identity_without_committed_effect()
-> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-invalid-effect-identity"},
        "spec": {
            "interface": {
                "outputs": {
                    "cancelled": "graphblocks.ai/AsyncOperationResult@1"
                }
            },
            "nodes": {
                "startCancel": {
                    "block": "async.start_operation@1",
                    "config": async_start_config("op-cancel", "node-cancel"),
                    "outputs": {"operation": "cancel.operation"}
                },
                "cancel": {
                    "block": "async.cancel_operation@1",
                    "config": {
                        "cancelledAtUnixMs": 1_900,
                        "externalEffects": [
                            {
                                "effectId": "effect-ticket-1",
                                "target": "ticket-system",
                                "operation": "ticket.create",
                                "outcome": "no_external_effect",
                                "providerEffectId": "ticket-123"
                            }
                        ]
                    },
                    "inputs": {"operation": "startCancel.operation"},
                    "outputs": {"result": "$output.cancelled"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    assert_eq!(
        result
            .pointer("/journal/4/payload/code")
            .and_then(Value::as_str),
        Some("async.operation_result.invalid_result")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_rejects_timeout_expiration_overflow() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-timeout-overflow"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": u64::MAX - 9,
                        "submittedAtUnixMs": u64::MAX - 8,
                        "timeoutMs": 20_u64,
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("timeout exceeds timestamp range")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_rejects_submitted_before_created() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-submitted-before-created"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 2_000,
                        "submittedAtUnixMs": 1_999,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("submitted_at precedes created_at")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_rejects_expiry_before_submission() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-expiry-before-submission"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "gha-run-1",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 2_500,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("expires_at must be after submitted_at")
    );
    Ok(())
}

#[test]
fn rust_stdlib_async_start_operation_rejects_wait_without_submission() -> Result<(), String> {
    let graph = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-async-start-wait-without-submission"},
        "spec": {
            "interface": {
                "outputs": {"operation": "graphblocks.ai/AsyncOperation@1"}
            },
            "nodes": {
                "startCI": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": "op-ci-1",
                        "runId": "run-coding-1",
                        "nodeId": "startCI",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "resumeTokenHash": "sha256:resume-token",
                        "idempotencyKey": "idem-op-ci-1",
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "timeoutMs": 1_000,
                        "resume": {
                            "requirePolicyReevaluation": true,
                            "requireBudgetReservation": true,
                            "requireReleaseCompatibility": true,
                            "requireOwnershipFence": true
                        },
                        "attemptFencing": true
                    },
                    "outputs": {"operation": "$output.operation"}
                }
            }
        }
    });

    let result = run_graph(&graph, &json!({}))?;

    assert_eq!(result["status"], "failed");
    let node_failed = result["journal"]
        .as_array()
        .and_then(|records| {
            records.iter().find(|record| {
                record.pointer("/kind").and_then(Value::as_str) == Some("node_failed")
            })
        })
        .ok_or_else(|| "missing node_failed journal record".to_string())?;
    assert!(
        node_failed
            .pointer("/payload/message")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .contains("non-created operations require submitted_at")
    );
    Ok(())
}

fn async_start_config(operation_id: &str, node_id: &str) -> Value {
    json!({
        "operationId": operation_id,
        "runId": "run-coding-1",
        "nodeId": node_id,
        "attemptId": "attempt-1",
        "kind": "ci_job",
        "providerOperationId": format!("provider-{operation_id}"),
        "resumeTokenHash": format!("sha256:resume-token-{operation_id}"),
        "idempotencyKey": format!("idem-{operation_id}"),
        "expectedSchema": "schemas/CICallback@1",
        "createdAtUnixMs": 1_000,
        "submittedAtUnixMs": 1_050,
        "expiresAtUnixMs": 2_000,
        "timeoutMs": 1_000,
        "resume": {
            "requirePolicyReevaluation": true,
            "requireBudgetReservation": true,
            "requireReleaseCompatibility": true,
            "requireOwnershipFence": true
        },
        "attemptFencing": true
    })
}

fn run_graph(graph: &Value, inputs: &Value) -> Result<Value, String> {
    let graph_json = serde_json::to_string(graph).map_err(|error| error.to_string())?;
    let inputs_json = serde_json::to_string(inputs).map_err(|error| error.to_string())?;
    let result_json =
        run_stdlib_graph_json(&graph_json, &inputs_json).map_err(|error| error.to_string())?;
    serde_json::from_str::<Value>(&result_json).map_err(|error| error.to_string())
}

fn resolved_tool_by_name<'a>(tools: &'a [Value], name: &str) -> Result<&'a Value, String> {
    tools
        .iter()
        .find(|tool| tool.pointer("/definition/name").and_then(Value::as_str) == Some(name))
        .ok_or_else(|| format!("resolved tool {name:?} was not emitted"))
}
