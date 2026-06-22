# Appendix B. Acceptance Application Pseudocode

## B.1 Federated enterprise RAG

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: enterprise-rag-turn
spec:
  interface:
    inputs:
      turn: graphblocks.ai/ConversationTurnInput@1
      auth: graphblocks.ai/AuthContext@1
    outputs:
      result: graphblocks.ai/TurnCandidate@1
    events:
      - graphblocks.ai/AssistantDraftDelta@1

  nodes:
    begin:
      block: conversation.begin_turn@1

    classify:
      block: query.classify@1

    rewrite:
      block: query.rewrite@1

    plan:
      block: query.plan_retrieval@1

    retrieve:
      block: retrieve.execute_plan@1
      bindings:
        retrievers:
          dense: company_dense
          keyword: company_keyword
          tickets: support_tickets
        embedding: query_embedding
      config:
        minimumSuccessfulSources: 1
        sourceTimeout: 2s

    fuse:
      block: retrieve.fuse@1
      config:
        algorithm: reciprocal_rank_fusion

    rerank:
      block: rank.documents@1
      bindings:
        reranker: answer_reranker

    context:
      block: context.build@1
      config:
        maxTokens: 48000
        reserveOutputTokens: 8000

    generate:
      block: model.generate@1
      bindings:
        model: answer_model
      projection:
        text: AssistantDraftDelta

    validate:
      block: answer.validate_grounding@1
      config:
        requireCitation: true
        onInsufficientEvidence: abstain

    commit:
      block: conversation.commit_turn@1
```

### B.1.1 Production BindingSpec

GraphSpec에는 logical resource name만 기록하고, provider·endpoint·credential은 별도 BindingSpec에서 해석한다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Binding
metadata:
  name: enterprise-rag-production
spec:
  resources:
    company_dense:
      kind: Retriever
      implementation: qdrant.dense
      config:
        collection: company_docs_v17
        endpoint: https://qdrant.internal
      credentials: {secretRef: secret://qdrant/production}

    company_keyword:
      kind: Retriever
      implementation: opensearch.keyword
      config:
        index: company_docs_v17
        endpoint: https://opensearch.internal
      credentials: {secretRef: secret://opensearch/production}

    support_tickets:
      kind: Retriever
      implementation: company.ticket_search
      config: {endpoint: https://tickets.internal/search}
      credentials: {secretRef: secret://tickets/production}

    query_embedding:
      kind: EmbeddingModel
      implementation: openai.embeddings
      config: {model: embedding-model-production}
      credentials: {secretRef: secret://openai/production}

    answer_reranker:
      kind: Reranker
      implementation: cross_encoder.remote
      config: {endpoint: https://reranker.internal}

    answer_model:
      kind: ChatModel
      implementation: openai.responses
      config: {model: chat-model-production}
      credentials: {secretRef: secret://openai/production}
```

## B.2 TUI workspace assistant

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: Application
metadata:
  name: workspace-assistant
spec:
  surfaces:
    default:
      kind: tui
      implementation: textual
      protocol: graphblocks.app.v1
  graphs:
    default: graphs/workspace-agent.yaml
  capabilities:
    - assistant_drafts
    - approval
    - artifact_preview
    - breakpoint_resume
```

Workspace graph는 `workspace.snapshot/context`, `agent.run`, `workspace.propose_patch`, `test.run`을 사용하고, patch 적용과 process 실행은 approval/sandbox policy를 요구한다.

## B.3 Durable document preprocessing

```yaml
nodes:
  snapshot:
    block: asset.snapshot_source@1

  diff:
    block: asset.diff_snapshot@1

  process:
    block: control.map@2
    config:
      graph: graphs/process-single-asset.yaml
      itemKey: $.revision_id
      concurrency: 16
      stateIsolation: item
      checkpoint: per_item
      onError: collect

  delete:
    block: control.map@2
    config:
      graph: graphs/delete-single-asset.yaml
      itemKey: $.revision_id
      checkpoint: per_item
```

Single asset graph는 begin revision, cache lookup, deterministic converter selection, quality/OCR fallback, normalize/redact/enrich, structured extraction, artifact/manifest/index staging, commit을 포함한다.

## B.4 Usage policy — finish-current-turn profile

이미 시작된 turn을 bounded overdraft 안에서 마치고 새 turn을 차단하는 profile이다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: PolicyProfile
metadata:
  name: interactive-graceful
spec:
  quotaAccounts:
    userInteractive:
      scope: principal
      window:
        kind: rolling
        duration: 5h
      limits:
        - kind: model_input_tokens
          hard: 200000
          unit: token
        - kind: model_output_tokens
          hard: 40000
          unit: token

  budgets:
    turn:
      inheritFrom: userInteractive
      reservation:
        required: true
        safetyMargin: 0.15
      completionReserve:
        - kind: model_output_tokens
          quantity: 2000
          unit: token

  thresholds:
    - at: 0.80
      actions: [notify]
    - at: 0.90
      actions: [prefer_economy_model, reduce_parallelism]

  exhaustion:
    preset: finish_current_turn
    denyNewWork: true
    inFlight: finish_current_unit
    unit: turn
    continuation:
      allowedWork: [already_admitted_child_work, declared_finalization, checkpoint, cleanup]
      forbiddenWork: [new_turn, plan_expansion, optional_task, state_changing_effect]
      maxAdditionalUsage:
        - {kind: model_output_tokens, quantity: 4000, unit: token}
        - {kind: wall_time_ms, quantity: 600000, unit: ms}
      maxAdditionalSteps: 2
      deadline: 10m
    maxOverdraft:
      - {kind: model_output_tokens, quantity: 4000, unit: token}
      - {kind: wall_time_ms, quantity: 600000, unit: ms}
    output:
      clientDelivery: continue_to_boundary
      durableResult: commit_with_exhaustion_notice
    effects: preserve_atomicity
    afterUnit: reject
```

## B.5 Usage policy — hard-stop profile

현재 provider call에 cancellation을 요청하고 미완성 draft를 retract하는 profile이다.

```yaml
apiVersion: graphblocks.ai/v1alpha1
kind: PolicyProfile
metadata:
  name: interactive-hard-stop
spec:
  quotaAccounts:
    userInteractive:
      scope: principal
      window: {kind: rolling, duration: 5h}
      limits:
        - {kind: model_input_tokens, hard: 200000, unit: token}
        - {kind: model_output_tokens, hard: 40000, unit: token}

  exhaustion:
    preset: hard_stop
    denyNewWork: true
    inFlight: cancel_immediately
    unit: provider_call
    continuation:
      allowedWork: [cleanup]
      forbiddenWork: [new_turn, plan_expansion, unreserved_provider_call, state_changing_effect]
    maxOverdraft: []
    output:
      clientDelivery: stop_immediately
      durableResult: retract
    effects: preserve_atomicity
    afterUnit: reject
```

`cancel_immediately`는 best-effort remote cancellation이다. 이미 effect commit critical section에 들어간 작업은 effect policy에 따라 마무리하거나 indeterminate/compensation 상태를 기록한다.

## B.6 Adaptive research orchestration budget

Research domain type을 core에 추가하지 않고 generic TaskPlan, EvidenceRef, Check/Gate, ResultBundle을 사용한다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: bounded-research-orchestrator
spec:
  interface:
    inputs:
      objective: company.research/Objective@1
      sources: list[graphblocks.core/SourceRef@1]
    outputs:
      result: graphblocks.core/ResultBundle@1

  nodes:
    snapshot:
      block: resource.snapshot@1

    plan:
      block: orchestration.plan@1
      config:
        outputSchema: graphblocks.orchestration/TaskPlan@1
        limits:
          maxTasks: 48
          maxDepth: 4
        phaseBudgets:
          planning: 0.10
          execution: 0.55
          verification: 0.20
          finalization: 0.15

    validatePlan:
      block: orchestration.validate_plan@1

    execute:
      block: orchestration.execute_task_plan@1
      config:
        checkpoint: each_task
        reservation: per_task
        onBudgetPressure:
          cancelPriorities: [optional, normal]
          preserve: [required, verification, finalization]

    verify:
      block: check.run_suite@1

    gate:
      block: gate.evaluate@1

    bundle:
      block: result.bundle@1
```

## B.7 RTL candidate trial with budget and scarce-resource lease

반도체/Verilog 타입은 application-local schema로 유지한다. GraphBlocks는 snapshot, ChangeSet, Trial, Check/Gate, Review, LeasePool 계약만 제공한다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: rtl-candidate-trial
spec:
  interface:
    inputs:
      candidate: company.hdl/PatchCandidate@1
      base: graphblocks.core/ResourceSnapshotRef@1
    outputs:
      trial: graphblocks.evaluation/TrialResult@1

  nodes:
    reserveTrialBudget:
      block: budget.reserve@1
      config:
        limits:
          - {kind: model_total_tokens, quantity: 30000, unit: token}
          - {kind: cpu_seconds, quantity: 3600, unit: second}
          - {kind: licensed_resource_seconds, quantity: 900, unit: second}

    fork:
      block: workspace.fork@1
      execution:
        requires: {isolation: sandbox}

    apply:
      block: workspace.apply_changeset@1

    fastChecks:
      block: check.run_suite@1
      config:
        checks: [lint, compile, smoke_simulation]
        stopOnFailure: true

    formal:
      block: check.run_suite@1
      when: fastChecks.passed
      flow:
        leasePool: formal-license
      config:
        checks: [formal_properties]

    synthesis:
      block: check.run_suite@1
      when: formal.hardGatePassed
      flow:
        leasePool: synthesis-license
      config:
        checks: [synthesis, timing, area]

    gate:
      block: gate.evaluate@1
      config:
        hardConstraints:
          - lint_passed
          - compile_passed
          - regression_passed
          - formal_not_failed
        objectives:
          - {metric: area, direction: minimize}
          - {metric: worst_slack, direction: maximize}

    seal:
      block: trial.seal_result@1
      policies:
        integrity: trusted-oracle-unchanged
        budget:
          onExhaustion:
            inFlight: checkpoint_then_pause
            unit: trial
```

