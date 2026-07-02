use graphblocks_runtime_core::deployment::{
    CallbackIngressConfig, DeploymentCondition, DeploymentEvent, DeploymentEventKind,
    DeploymentObservabilityContext, DeploymentRecoveryProfile, DeploymentRevision,
    DeploymentSloProfile, DeploymentSloReport, DeploymentTargetProfile, DeploymentTargetProfileSet,
    ExecutionTarget, ExecutionTargetKind, GraphRelease, GraphReleaseError, GraphReleaseGraph,
    ImageRef, KnowledgeBinding, KubernetesTargetRenderer, PhysicalExecutionPlan, PlacementError,
    PlacementRule, PlacementSelector, PromptLock, ReleaseLockRef, RevisionDecision,
    RolloutAnalysisResult, RolloutPlan, RolloutStep, SupplyChainLock, UpgradePolicy,
    WorkerAdmissionError, WorkerAdmissionRequirement, WorkerAdvertisement, WorkloadKind,
};
use serde_json::json;

#[test]
fn deployment_revision_digest_is_stable_without_record_identity() {
    let left = DeploymentRevision::new(
        "rev-1",
        "sha256:release",
        "sha256:deployment",
        "sha256:physical",
        "sha256:binding",
        "sha256:target",
        "2026-06-23T00:00:00Z",
    );
    let right = DeploymentRevision::new(
        "rev-2",
        "sha256:release",
        "sha256:deployment",
        "sha256:physical",
        "sha256:binding",
        "sha256:target",
        "2026-06-23T00:01:00Z",
    );

    assert_eq!(left.content_digest(), right.content_digest());
    assert_eq!(left.revision_id, "rev-1");
    assert_eq!(left.release_digest, "sha256:release");
}

#[test]
fn physical_plan_hash_is_stable_for_target_and_rule_order() {
    let control = ExecutionTarget::new("control", ExecutionTargetKind::Service, "rust")
        .with_capabilities(["graph.coordinator", "model.remote_call"]);
    let doc_cpu = ExecutionTarget::new("doc-cpu", ExecutionTargetKind::WorkerPool, "python_worker")
        .with_capabilities(["document.parse.pdf"]);
    let left = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(doc_cpu.clone())
        .with_target(control.clone())
        .with_placement(PlacementRule::new(
            "docs",
            PlacementSelector::capabilities(["document.parse.pdf"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "generate",
            PlacementSelector::nodes(["generate"]),
            "control",
        ));
    let right = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_package_lock_hash("sha256:package")
        .with_target(control)
        .with_target(doc_cpu)
        .with_placement(PlacementRule::new(
            "generate",
            PlacementSelector::nodes(["generate"]),
            "control",
        ))
        .with_placement(PlacementRule::new(
            "docs",
            PlacementSelector::capabilities(["document.parse.pdf"]),
            "doc-cpu",
        ));

    assert_eq!(left.plan_hash(), right.plan_hash());
}

#[test]
fn deployment_target_profiles_cover_phase_five_image_roles() {
    let target_set = DeploymentTargetProfileSet::from_document(&json!({
        "kind": "DeploymentTargetProfileSet",
        "spec": {
            "targets": [
                {
                    "id": "control",
                    "imageRole": "control-plane",
                    "kind": "service",
                    "executionHost": "rust",
                    "capabilities": [
                        "graph.coordinator",
                        "model.remote_call",
                        "retrieval.remote_call"
                    ],
                    "effects": ["network"],
                    "packageLock": "locks/control.lock",
                    "defaultReplicas": 2
                },
                {
                    "id": "rag-cpu",
                    "imageRole": "rag-cpu",
                    "kind": "worker_pool",
                    "executionHost": "python_worker",
                    "capabilities": ["rag.context", "retrieval.local", "rerank.local"],
                    "effects": ["network"],
                    "packageLock": "locks/rag-cpu.lock",
                    "defaultReplicas": 2
                },
                {
                    "id": "document-cpu",
                    "imageRole": "document-cpu",
                    "kind": "worker_pool",
                    "executionHost": "python_worker",
                    "capabilities": [
                        "document.normalize",
                        "document.parse.office",
                        "document.parse.pdf",
                        "document.split"
                    ],
                    "effects": ["filesystem_read"],
                    "packageLock": "locks/document-cpu.lock",
                    "defaultReplicas": 2
                },
                {
                    "id": "ocr-gpu",
                    "imageRole": "ocr-gpu",
                    "kind": "worker_pool",
                    "executionHost": "python_worker",
                    "capabilities": ["document.ocr", "document.parse.image"],
                    "effects": ["filesystem_read"],
                    "packageLock": "locks/ocr-gpu.lock",
                    "defaultReplicas": 1
                },
                {
                    "id": "sandbox",
                    "imageRole": "sandbox",
                    "kind": "sandbox_pool",
                    "executionHost": "python_worker",
                    "capabilities": ["code.exec", "tool.sandbox"],
                    "effects": ["filesystem_write", "process_execution"],
                    "packageLock": "locks/sandbox.lock",
                    "defaultReplicas": 1
                }
            ]
        }
    }))
    .expect("phase five deployment target profiles parse");

    let coverage = target_set.coverage_for_required_image_roles([
        "control-plane",
        "rag-cpu",
        "document-cpu",
        "ocr-gpu",
        "sandbox",
    ]);

    assert!(coverage.ok());
    assert!(coverage.issue_contracts().is_empty());
    assert_eq!(
        target_set.image_roles(),
        vec![
            "control-plane",
            "rag-cpu",
            "document-cpu",
            "ocr-gpu",
            "sandbox",
        ]
    );
    assert_eq!(
        target_set.target_ids(),
        vec!["control", "document-cpu", "ocr-gpu", "rag-cpu", "sandbox"]
    );
}

#[test]
fn deployment_target_profiles_project_to_execution_targets() {
    let target_set = DeploymentTargetProfileSet::from_document(&json!({
        "kind": "DeploymentTargetProfileSet",
        "spec": {
            "targets": [{
                "id": "control",
                "imageRole": "control-plane",
                "kind": "service",
                "executionHost": "rust",
                "capabilities": [
                    "retrieval.remote_call",
                    "graph.coordinator",
                    "model.remote_call"
                ],
                "effects": ["network"],
                "packageLock": "locks/control.lock",
                "defaultReplicas": 2
            }]
        }
    }))
    .expect("deployment target profiles parse");
    let control = target_set
        .by_id("control")
        .expect("control target profile exists");

    let target = control
        .to_execution_target("registry.example.com/gb/control@sha256:control")
        .expect("control target profile projects to an execution target");

    assert_eq!(
        control.profile_contract(),
        json!({
            "target_id": "control",
            "image_role": "control-plane",
            "kind": "service",
            "execution_host": "rust",
            "capabilities": [
                "graph.coordinator",
                "model.remote_call",
                "retrieval.remote_call"
            ],
            "effects": ["network"],
            "package_lock": "locks/control.lock",
            "default_replicas": 2
        })
    );
    assert_eq!(target.target_id, "control");
    assert_eq!(target.kind, ExecutionTargetKind::Service);
    assert_eq!(target.execution_host, "rust");
    assert_eq!(
        target.capabilities.into_iter().collect::<Vec<_>>(),
        vec![
            "graph.coordinator".to_owned(),
            "model.remote_call".to_owned(),
            "retrieval.remote_call".to_owned(),
        ]
    );
    assert_eq!(
        target.effects.into_iter().collect::<Vec<_>>(),
        vec!["network"]
    );
    assert_eq!(target.package_lock.as_deref(), Some("locks/control.lock"));
    assert_eq!(
        target.image.as_deref(),
        Some("registry.example.com/gb/control@sha256:control")
    );
    assert!(target_set.content_digest().starts_with("sha256:"));
}

#[test]
fn kubernetes_renderer_projects_service_target_to_deployment_and_service() {
    let profile = DeploymentTargetProfileSet::from_document(&json!({
        "kind": "DeploymentTargetProfileSet",
        "spec": {
            "targets": [{
                "id": "control",
                "imageRole": "control-plane",
                "kind": "service",
                "executionHost": "rust",
                "capabilities": ["graph.coordinator"],
                "effects": ["network"],
                "packageLock": "locks/control.lock",
                "defaultReplicas": 2
            }]
        }
    }))
    .expect("profile parses")
    .by_id("control")
    .expect("control profile exists")
    .clone();

    let manifests = KubernetesTargetRenderer::new("graphblocks")
        .render_target_profile(&profile, "registry.example.com/gb/control@sha256:control")
        .expect("service target renders");

    assert_eq!(
        manifests,
        vec![
            json!({
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": "control",
                    "namespace": "graphblocks",
                    "labels": {
                        "app.kubernetes.io/name": "graphblocks",
                        "graphblocks.ai/target-id": "control",
                        "graphblocks.ai/image-role": "control-plane"
                    }
                },
                "spec": {
                    "replicas": 2,
                    "selector": {
                        "matchLabels": {
                            "graphblocks.ai/target-id": "control"
                        }
                    },
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": "graphblocks",
                                "graphblocks.ai/target-id": "control",
                                "graphblocks.ai/image-role": "control-plane"
                            }
                        },
                        "spec": {
                            "containers": [{
                                "name": "control",
                                "image": "registry.example.com/gb/control@sha256:control",
                                "env": [
                                    {"name": "GRAPHBLOCKS_TARGET_ID", "value": "control"},
                                    {"name": "GRAPHBLOCKS_IMAGE_ROLE", "value": "control-plane"},
                                    {"name": "GRAPHBLOCKS_EXECUTION_HOST", "value": "rust"},
                                    {"name": "GRAPHBLOCKS_PACKAGE_LOCK", "value": "locks/control.lock"}
                                ]
                            }]
                        }
                    }
                }
            }),
            json!({
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "control",
                    "namespace": "graphblocks",
                    "labels": {
                        "app.kubernetes.io/name": "graphblocks",
                        "graphblocks.ai/target-id": "control",
                        "graphblocks.ai/image-role": "control-plane"
                    }
                },
                "spec": {
                    "selector": {
                        "graphblocks.ai/target-id": "control"
                    },
                    "ports": [{
                        "name": "http",
                        "port": 8080,
                        "targetPort": 8080
                    }]
                }
            })
        ]
    );
}

#[test]
fn kubernetes_renderer_projects_worker_pool_without_service() {
    let profile = DeploymentTargetProfile::new(
        "document-cpu",
        "document-cpu",
        ExecutionTargetKind::WorkerPool,
        "python_worker",
    )
    .expect("profile is valid")
    .with_capabilities(["document.parse.pdf"])
    .with_default_replicas(3);

    let manifests = KubernetesTargetRenderer::new("graphblocks")
        .render_target_profile(&profile, "registry.example.com/gb/document@sha256:document")
        .expect("worker target renders");

    assert_eq!(manifests.len(), 1);
    assert_eq!(manifests[0]["kind"], "Deployment");
    assert_eq!(manifests[0]["metadata"]["name"], "document-cpu");
    assert_eq!(manifests[0]["spec"]["replicas"], 3);
    assert_eq!(
        manifests[0]["spec"]["template"]["spec"]["containers"][0]["env"],
        json!([
            {"name": "GRAPHBLOCKS_TARGET_ID", "value": "document-cpu"},
            {"name": "GRAPHBLOCKS_IMAGE_ROLE", "value": "document-cpu"},
            {"name": "GRAPHBLOCKS_EXECUTION_HOST", "value": "python_worker"}
        ])
    );
}

#[test]
fn callback_ingress_manifest_parses_security_limits_and_routes() {
    let ingress = CallbackIngressConfig::from_document(&json!({
        "enabled": true,
        "routes": [
            {
                "path": "/v1/callbacks/{operation_id}",
                "command": "SubmitAsyncCallback"
            }
        ],
        "security": {
            "requireSignature": true,
            "antiEnumeration": true
        },
        "limits": {
            "maxPayloadBytes": 262144,
            "maxRequestsPerSecond": 100
        }
    }))
    .expect("callback ingress manifest parses");

    assert!(ingress.enabled);
    assert!(ingress.diagnostics().is_empty());
    assert_eq!(
        ingress.manifest_contract(),
        json!({
            "enabled": true,
            "routes": [
                {
                    "path": "/v1/callbacks/{operation_id}",
                    "command": "SubmitAsyncCallback"
                }
            ],
            "security": {
                "require_signature": true,
                "anti_enumeration": true
            },
            "limits": {
                "max_payload_bytes": 262144,
                "max_requests_per_second": 100
            }
        })
    );
    assert!(ingress.content_digest().starts_with("sha256:"));
}

#[test]
fn callback_ingress_diagnostics_require_signature_when_enabled() {
    let ingress = CallbackIngressConfig::from_document(&json!({
        "enabled": true,
        "routes": [
            {
                "path": "/v1/callbacks/{operation_id}",
                "command": "SubmitAsyncCallback"
            }
        ],
        "security": {
            "requireSignature": false,
            "antiEnumeration": true
        },
        "limits": {
            "maxPayloadBytes": 262144,
            "maxRequestsPerSecond": 100
        }
    }))
    .expect("callback ingress manifest parses");

    let diagnostics = ingress.diagnostics();

    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0].code, "GB6002");
    assert_eq!(
        diagnostics[0].field,
        "callbackIngress.security.requireSignature"
    );
}

#[test]
fn callback_ingress_requires_submit_async_callback_route() {
    let error = CallbackIngressConfig::from_document(&json!({
        "enabled": true,
        "routes": [
            {
                "path": "/v1/other",
                "command": "GetRunStatus"
            }
        ],
        "security": {
            "requireSignature": true,
            "antiEnumeration": true
        },
        "limits": {
            "maxPayloadBytes": 262144,
            "maxRequestsPerSecond": 100
        }
    }))
    .expect_err("callback ingress must expose SubmitAsyncCallback");

    assert!(
        error
            .to_string()
            .contains("enabled callback ingress requires a SubmitAsyncCallback route")
    );
}

#[test]
fn deployment_target_coverage_reports_missing_image_role() {
    let target_set = DeploymentTargetProfileSet::new([]);

    let coverage = target_set.coverage_for_required_image_roles(["control-plane"]);

    assert!(!coverage.ok());
    assert_eq!(
        coverage.issue_contracts(),
        vec![json!({
            "code": "DeploymentTargetRoleMissing",
            "image_role": "control-plane",
            "target_id": "",
            "path": "$.spec.targets",
            "message": "required production image role has no deployment target profile",
        })]
    );
}

#[test]
fn placement_resolution_applies_priority_from_node_to_default() -> Result<(), PlacementError> {
    let plan = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_target(
            ExecutionTarget::new("control", ExecutionTargetKind::Service, "rust")
                .with_capabilities(["graph.coordinator"]),
        )
        .with_target(
            ExecutionTarget::new("doc-cpu", ExecutionTargetKind::WorkerPool, "python_worker")
                .with_capabilities(["document.parse.pdf"]),
        )
        .with_target(
            ExecutionTarget::new("sandbox", ExecutionTargetKind::SandboxPool, "python_worker")
                .with_effects(["process_execution"]),
        )
        .with_default_target("control")
        .with_placement(PlacementRule::new(
            "group-doc",
            PlacementSelector::execution_groups(["per-document"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "block-doc",
            PlacementSelector::blocks(["document.parse"]),
            "doc-cpu",
        ))
        .with_placement(PlacementRule::new(
            "effect-sandbox",
            PlacementSelector::effects(["process_execution"]),
            "sandbox",
        ))
        .with_placement(PlacementRule::new(
            "node-control",
            PlacementSelector::nodes(["parse-one"]),
            "control",
        ));

    assert_eq!(
        plan.resolve_target(
            "parse-one",
            Some("per-document"),
            "document.parse",
            ["document.parse.pdf"],
            ["process_execution"],
            Some("batch"),
        )?
        .target_id,
        "control",
    );
    assert_eq!(
        plan.resolve_target(
            "parse-two",
            Some("per-document"),
            "document.parse",
            ["document.parse.pdf"],
            std::iter::empty::<&str>(),
            None,
        )?
        .target_id,
        "doc-cpu",
    );
    assert_eq!(
        plan.resolve_target(
            "other",
            None,
            "value.const",
            std::iter::empty::<&str>(),
            std::iter::empty::<&str>(),
            None,
        )?
        .target_id,
        "control",
    );
    Ok(())
}

#[test]
fn placement_resolution_rejects_same_priority_conflicts() {
    let plan = PhysicalExecutionPlan::new("sha256:release", "rev-1", "sha256:graph")
        .with_target(ExecutionTarget::new(
            "control",
            ExecutionTargetKind::Service,
            "rust",
        ))
        .with_target(ExecutionTarget::new(
            "doc-cpu",
            ExecutionTargetKind::WorkerPool,
            "python_worker",
        ))
        .with_placement(PlacementRule::new(
            "a",
            PlacementSelector::nodes(["generate"]),
            "control",
        ))
        .with_placement(PlacementRule::new(
            "b",
            PlacementSelector::nodes(["generate"]),
            "doc-cpu",
        ));

    assert_eq!(
        plan.resolve_target(
            "generate",
            None,
            "model.generate",
            std::iter::empty::<&str>(),
            std::iter::empty::<&str>(),
            None,
        ),
        Err(PlacementError::AmbiguousPlacement {
            node_id: "generate".to_owned(),
            priority: "node".to_owned(),
            target_ids: vec!["control".to_owned(), "doc-cpu".to_owned()],
        })
    );
}

#[test]
fn worker_admission_rejects_incompatible_protocol_or_package_lock() {
    let requirement = WorkerAdmissionRequirement::new("sandbox", "worker-protocol/1")
        .with_package_lock_hash("sha256:package-a")
        .with_required_capabilities(["code.exec", "tool.sandbox"]);
    let wrong_protocol = WorkerAdvertisement::new("worker-1", "sandbox", "worker-protocol/2")
        .with_package_lock_hash("sha256:package-a")
        .with_capabilities(["code.exec", "tool.sandbox"]);
    let wrong_package = WorkerAdvertisement::new("worker-2", "sandbox", "worker-protocol/1")
        .with_package_lock_hash("sha256:package-b")
        .with_capabilities(["code.exec", "tool.sandbox"]);

    assert_eq!(
        requirement.admit(&wrong_protocol),
        Err(WorkerAdmissionError::ProtocolMismatch {
            worker_id: "worker-1".to_owned(),
            expected: "worker-protocol/1".to_owned(),
            actual: "worker-protocol/2".to_owned(),
        })
    );
    assert_eq!(
        requirement.admit(&wrong_package),
        Err(WorkerAdmissionError::PackageLockMismatch {
            worker_id: "worker-2".to_owned(),
            expected: "sha256:package-a".to_owned(),
            actual: Some("sha256:package-b".to_owned()),
        })
    );
}

#[test]
fn worker_admission_rejects_wrong_target_or_missing_capability() {
    let requirement = WorkerAdmissionRequirement::new("document-cpu", "worker-protocol/1")
        .with_required_capabilities(["document.parse.pdf", "document.normalize"]);
    let wrong_target = WorkerAdvertisement::new("worker-1", "sandbox", "worker-protocol/1")
        .with_capabilities(["document.parse.pdf", "document.normalize"]);
    let missing_capability =
        WorkerAdvertisement::new("worker-2", "document-cpu", "worker-protocol/1")
            .with_capabilities(["document.parse.pdf"]);

    assert_eq!(
        requirement.admit(&wrong_target),
        Err(WorkerAdmissionError::TargetMismatch {
            worker_id: "worker-1".to_owned(),
            expected: "document-cpu".to_owned(),
            actual: "sandbox".to_owned(),
        })
    );
    assert_eq!(
        requirement.admit(&missing_capability),
        Err(WorkerAdmissionError::MissingCapabilities {
            worker_id: "worker-2".to_owned(),
            missing: vec!["document.normalize".to_owned()],
        })
    );
}

#[test]
fn worker_admission_accepts_matching_worker_advertisement() {
    let requirement = WorkerAdmissionRequirement::new("rag-cpu", "worker-protocol/1")
        .with_package_lock_hash("sha256:package")
        .with_required_capabilities(["rag.context"]);
    let worker = WorkerAdvertisement::new("worker-1", "rag-cpu", "worker-protocol/1")
        .with_package_lock_hash("sha256:package")
        .with_capabilities(["rag.context", "retrieval.local"]);

    requirement
        .admit(&worker)
        .expect("compatible worker should be admitted");
}

#[test]
fn graph_release_digest_is_stable_for_artifact_order() {
    let left = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_graph(
            "ingest",
            GraphReleaseGraph::new("sha256:graph-ingest", "sha256:plan-ingest"),
        )
        .with_image(
            "worker",
            ImageRef::new("registry.example.com/gb/worker@sha256:abc"),
        )
        .with_prompt_lock(
            "answer",
            PromptLock::versioned("support.answer", "2026-06-23"),
        );
    let right = GraphRelease::new("enterprise-rag-copy", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_application_hash("sha256:app")
        .with_graph(
            "ingest",
            GraphReleaseGraph::new("sha256:graph-ingest", "sha256:plan-ingest"),
        )
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_prompt_lock(
            "answer",
            PromptLock::versioned("support.answer", "2026-06-23"),
        )
        .with_image(
            "worker",
            ImageRef::new("registry.example.com/gb/worker@sha256:abc"),
        );

    assert_eq!(left.content_digest(), right.content_digest());
}

#[test]
fn graph_release_validation_rejects_mutable_production_references() {
    let release = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("latest", "application/vnd.graphblocks.release.v1")
        .with_graph("chat", GraphReleaseGraph::new("main", "sha256:plan-chat"))
        .with_image(
            "control",
            ImageRef::new("registry.example.com/gb/control:latest"),
        )
        .with_prompt_lock("answer", PromptLock::label("support.answer", "production"))
        .with_knowledge(KnowledgeBinding::new("intranet_docs", "current"))
        .with_lock("python", ReleaseLockRef::new("pylock.toml"))
        .with_supply_chain(SupplyChainLock::new(
            Some("oci://registry/sbom:latest"),
            Some("oci://registry/provenance:latest"),
            Some("production-publishers"),
        ));

    assert_eq!(
        release.validate_production_pins(),
        Err(GraphReleaseError::MutableReferences {
            references: vec![
                "bundle.digest".to_owned(),
                "graphs.chat.graph_hash".to_owned(),
                "images.control".to_owned(),
                "locks.python.digest".to_owned(),
                "knowledge.intranet_docs.index_revision".to_owned(),
                "prompts.answer".to_owned(),
                "supply_chain.provenance_ref".to_owned(),
                "supply_chain.sbom_ref".to_owned(),
            ],
        })
    );
}

#[test]
fn graph_release_supply_chain_lock_is_part_of_release_digest_and_production_pins() {
    let base = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_supply_chain(SupplyChainLock::new(
            Some("oci://registry/sbom@sha256:sbom"),
            Some("oci://registry/provenance@sha256:provenance"),
            Some("production-publishers"),
        ));
    let changed_policy = base.clone().with_supply_chain(SupplyChainLock::new(
        Some("oci://registry/sbom@sha256:sbom"),
        Some("oci://registry/provenance@sha256:provenance"),
        Some("staging-publishers"),
    ));

    assert_eq!(base.validate_production_pins(), Ok(()));
    assert_eq!(
        base.supply_chain
            .as_ref()
            .map(SupplyChainLock::canonical_value),
        Some(json!({
            "sbom_ref": "oci://registry/sbom@sha256:sbom",
            "provenance_ref": "oci://registry/provenance@sha256:provenance",
            "signature_policy": "production-publishers",
        }))
    );
    assert_ne!(base.content_digest(), changed_policy.content_digest());
}

#[test]
fn graph_release_lock_refs_are_part_of_release_digest_and_production_pins() {
    let base = GraphRelease::new("enterprise-rag", "2026.06.23.1")
        .with_bundle("sha256:bundle", "application/vnd.graphblocks.release.v1")
        .with_graph(
            "chat",
            GraphReleaseGraph::new("sha256:graph-chat", "sha256:plan-chat"),
        )
        .with_lock(
            "python",
            ReleaseLockRef::new("locks/pylock.toml")
                .with_digest("sha256:pylock")
                .with_lock_type("package"),
        )
        .with_lock(
            "policies",
            ReleaseLockRef::new("oci://registry/policies@sha256:policy-lock")
                .with_lock_type("policy"),
        );
    let changed_lock = base.clone().with_lock(
        "python",
        ReleaseLockRef::new("locks/pylock.toml")
            .with_digest("sha256:other-pylock")
            .with_lock_type("package"),
    );

    assert_eq!(base.validate_production_pins(), Ok(()));
    assert_eq!(
        base.locks
            .get("python")
            .map(ReleaseLockRef::canonical_value),
        Some(json!({
            "ref": "locks/pylock.toml",
            "digest": "sha256:pylock",
            "lock_type": "package",
        }))
    );
    assert_ne!(base.content_digest(), changed_lock.content_digest());
}

#[test]
fn deployment_event_exports_release_revision_and_cohort_attributes() {
    let context = DeploymentObservabilityContext::new("release-1", "rev-1")
        .with_release_digest("sha256:release")
        .with_rollout("rollout-1", "step-2", "canary");
    let event = DeploymentEvent::new(
        "event-1",
        DeploymentEventKind::RolloutGateFailed,
        context,
        "2026-06-23T00:00:00Z",
    )
    .with_metadata("reason", json!("latency_regression"));

    let attributes = event.telemetry_attributes();

    assert_eq!(
        DeploymentEventKind::RolloutGateFailed.as_str(),
        "rollout.gate.failed"
    );
    assert_eq!(
        attributes.get("deployment.event").map(String::as_str),
        Some("rollout.gate.failed")
    );
    assert_eq!(
        attributes.get("graphblocks.release.id").map(String::as_str),
        Some("release-1")
    );
    assert_eq!(
        attributes
            .get("graphblocks.deployment.revision")
            .map(String::as_str),
        Some("rev-1")
    );
    assert_eq!(
        attributes
            .get("graphblocks.rollout.cohort")
            .map(String::as_str),
        Some("canary")
    );
    assert_eq!(
        event.metadata.get("reason"),
        Some(&json!("latency_regression"))
    );
}

#[test]
fn deployment_observability_context_compares_stable_and_canary_rollout_step() {
    let stable = DeploymentObservabilityContext::new("release-stable", "rev-stable").with_rollout(
        "rollout-1",
        "step-2",
        "stable",
    );
    let canary = DeploymentObservabilityContext::new("release-canary", "rev-canary").with_rollout(
        "rollout-1",
        "step-2",
        "canary",
    );
    let later = DeploymentObservabilityContext::new("release-canary", "rev-canary").with_rollout(
        "rollout-1",
        "step-3",
        "canary",
    );

    assert!(stable.same_rollout_step(&canary));
    assert!(!stable.same_rollout_step(&later));
    assert_ne!(stable.cohort.as_deref(), canary.cohort.as_deref());
}

#[test]
fn upgrade_policy_finishes_existing_requests_on_old_revision() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(WorkloadKind::ExistingRequest, None, false),
        RevisionDecision::FinishOnOld {
            revision_id: "rev-old".to_owned(),
        }
    );
}

#[test]
fn upgrade_policy_preserves_conversation_affinity() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(
            WorkloadKind::Conversation,
            Some("rev-conversation-affinity"),
            false,
        ),
        RevisionDecision::KeepAffinity {
            revision_id: "rev-conversation-affinity".to_owned(),
        }
    );
    assert_eq!(
        policy.decide(WorkloadKind::Conversation, None, false),
        RevisionDecision::AdmitOnNew {
            revision_id: "rev-new".to_owned(),
        }
    );
}

#[test]
fn upgrade_policy_migrates_compatible_durable_jobs_and_drains_realtime_on_old() {
    let policy = UpgradePolicy::workload_aware("rev-old", "rev-new");

    assert_eq!(
        policy.decide(WorkloadKind::DurableJob, Some("rev-old"), true),
        RevisionDecision::CheckpointAndMigrate {
            from_revision_id: "rev-old".to_owned(),
            to_revision_id: "rev-new".to_owned(),
        }
    );
    assert_eq!(
        policy.decide(WorkloadKind::RealtimeSession, Some("rev-old"), true),
        RevisionDecision::DrainOnOld {
            revision_id: "rev-old".to_owned(),
        }
    );
}

#[test]
fn rollout_plan_builds_validate_shadow_canary_and_promote_sequence() {
    let plan = RolloutPlan::canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        [
            RolloutStep::canary("canary-1", 1).with_minimum_samples(200),
            RolloutStep::canary("canary-10", 10).with_minimum_duration_seconds(1800),
        ],
    )
    .with_affinity("conversation_id")
    .with_analysis_profile("rag-production-rollout");

    assert_eq!(
        plan.steps
            .iter()
            .map(|step| step.step_id.as_str())
            .collect::<Vec<_>>(),
        vec!["validate", "shadow", "canary-1", "canary-10", "promote"]
    );
    assert_eq!(plan.steps[1].effects, "suppress");
    assert_eq!(plan.steps[2].traffic_percent, 1);
    assert_eq!(
        plan.current_step(2)
            .expect("canary rollout step exists")
            .step_id,
        "canary-1"
    );
    assert_eq!(
        plan.analysis_profile_ref.as_deref(),
        Some("rag-production-rollout")
    );
    assert_eq!(plan.affinity.as_deref(), Some("conversation_id"));
}

#[test]
fn rollout_gate_holds_until_minimum_samples_and_duration_are_met() {
    let plan = RolloutPlan::canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        [RolloutStep::canary("canary-10", 10)
            .with_minimum_samples(20)
            .with_minimum_duration_seconds(60)],
    );
    let state = plan
        .initial_state()
        .advance_for_test(2)
        .expect("rollout advances to the canary gate");

    let held = state
        .evaluate_gate(
            RolloutAnalysisResult::passed("canary-10")
                .with_sample_count(19)
                .with_duration_seconds(120),
        )
        .expect("rollout gate evaluates held result");
    let advanced = state
        .evaluate_gate(
            RolloutAnalysisResult::passed("canary-10")
                .with_sample_count(20)
                .with_duration_seconds(60),
        )
        .expect("rollout gate evaluates advanced result");

    assert_eq!(held.decision, "hold");
    assert_eq!(held.reason, "minimum_samples_not_met");
    assert_eq!(held.next_state.current_step_index, 2);
    assert_eq!(advanced.decision, "advance");
    assert_eq!(advanced.next_state.current_step_index, 3);
}

#[test]
fn rollout_gate_promotes_after_final_promote_step_passes() {
    let plan = RolloutPlan::canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        [RolloutStep::canary("canary-50", 50)],
    );
    let state = plan
        .initial_state()
        .advance_for_test(3)
        .expect("rollout advances to final promote step");

    let decision = state
        .evaluate_gate(RolloutAnalysisResult::passed("promote"))
        .expect("rollout gate promotes after final step passes");

    assert_eq!(decision.decision, "promote");
    assert_eq!(decision.next_state.status, "promoted");
    assert_eq!(decision.next_state.current_step_index, 3);
}

#[test]
fn rollout_gate_aborts_without_automatic_rollback_for_non_reversible_effects() {
    let plan = RolloutPlan::canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        [RolloutStep::canary("canary-10", 10)],
    );
    let state = plan
        .initial_state()
        .advance_for_test(2)
        .expect("rollout advances to canary gate");

    let decision = state
        .evaluate_gate(
            RolloutAnalysisResult::failed("canary-10", "quality_gate_failed")
                .with_non_reversible_effect_observed(true),
        )
        .expect("rollout gate evaluates non-reversible failure");

    assert_eq!(decision.decision, "abort");
    assert_eq!(decision.reason, "quality_gate_failed");
    assert!(!decision.automatic_rollback_allowed);
    assert_eq!(decision.next_state.status, "aborted");
}

#[test]
fn deployment_slo_profile_evaluates_slo_within_budget_condition() {
    let profile = DeploymentSloProfile::new("rag-production", ["availability", "p95-latency"]);

    let condition = profile.evaluate_slo_reports([
        DeploymentSloReport::passed("availability"),
        DeploymentSloReport::failed("p95-latency"),
    ]);

    assert_eq!(
        condition,
        DeploymentCondition::new(
            "SLOWithinBudget",
            "false",
            "slo_failed",
            "failed SLO objectives: p95-latency",
        )
        .expect("failed SLO deployment condition is valid")
    );
    assert!(profile.content_digest().starts_with("sha256:"));
}

#[test]
fn deployment_slo_profile_reports_missing_or_no_data_as_unknown() {
    let profile = DeploymentSloProfile::new("rag-production", ["availability", "p95-latency"]);

    let condition = profile.evaluate_slo_reports([DeploymentSloReport::no_data("availability")]);

    assert_eq!(condition.condition_type, "SLOWithinBudget");
    assert_eq!(condition.status, "unknown");
    assert_eq!(condition.reason, "slo_no_data");
    assert_eq!(
        condition.message,
        "missing or no-data SLO objectives: availability, p95-latency"
    );
}

#[test]
fn deployment_recovery_profile_evaluates_restore_test_freshness() {
    let profile = DeploymentRecoveryProfile::new("production-recovery")
        .with_objective("service", "15m", "5m")
        .with_objective("durable_jobs", "1h", "checkpoint")
        .with_knowledge_index_sources(["source_assets", "manifests", "release_bundle"])
        .with_regional_failover("active_passive")
        .with_max_restore_test_age_seconds(86_400);

    let current = profile.evaluate_restore_test(Some(1_000), 80_000, true);
    let stale = profile.evaluate_restore_test(Some(1_000), 90_000, true);

    assert_eq!(
        current,
        DeploymentCondition::new("RecoveryTestCurrent", "true", "restore_test_current", "")
            .expect("current recovery deployment condition is valid")
    );
    assert_eq!(
        stale,
        DeploymentCondition::new(
            "RecoveryTestCurrent",
            "false",
            "restore_test_stale",
            "last restore test age 89000s exceeds 86400s",
        )
        .expect("stale recovery deployment condition is valid")
    );
    assert_eq!(
        profile.recovery_contract(),
        json!({
            "profile_id": "production-recovery",
            "objectives": [
                {"target": "durable_jobs", "rto": "1h", "rpo": "checkpoint"},
                {"target": "service", "rto": "15m", "rpo": "5m"},
            ],
            "knowledge_index_rebuildable_from": [
                "manifests",
                "release_bundle",
                "source_assets",
            ],
            "regional_failover_mode": "active_passive",
            "max_restore_test_age_seconds": 86400,
        })
    );
    assert!(profile.content_digest().starts_with("sha256:"));
}

#[test]
fn rollout_traffic_assignment_is_deterministic_and_sticky_by_affinity() {
    let plan = RolloutPlan::canary(
        "rollout-1",
        "rev-stable",
        "rev-canary",
        [RolloutStep::canary("canary-10", 10)],
    )
    .with_affinity("conversation_id");
    let step = plan
        .current_step(2)
        .expect("canary rollout step exists for assignment");

    let first = plan.assign_revision("conversation-1", step);
    let second = plan.assign_revision("conversation-1", step);

    assert_eq!(first, second);
    assert_eq!(
        plan.assign_revision("conversation-1", &RolloutStep::canary("stable", 0)),
        "rev-stable"
    );
    assert_eq!(
        plan.assign_revision("conversation-1", &RolloutStep::canary("candidate", 100)),
        "rev-canary"
    );
}
