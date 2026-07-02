# GraphBlocks v1.0 Implementation Plan

## 1. 목표

이 계획의 목표는 v1.0 명세를 기능 목록 순서가 아니라 **실행 가능한 vertical slice와 적합성 프로필 순서**로 구현하는 것이다. 초기부터 모든 provider, parser, connector, Kubernetes operator를 구현하지 않는다. Core semantics와 TCK가 먼저다.

## 2. Repository 구조

```text
graphblocks/
├─ crates/
│  ├─ graphblocks-schema
│  ├─ graphblocks-types
│  ├─ graphblocks-compiler
│  ├─ graphblocks-runtime-core
│  ├─ graphblocks-runtime-seq
│  ├─ graphblocks-runtime-durable
│  ├─ graphblocks-flow
│  ├─ graphblocks-telemetry
│  ├─ graphblocks-protocol
│  ├─ graphblocks-python
│  ├─ graphblocks-cli-native
│  └─ graphblocksd
├─ packages/
│  ├─ graphblocks-core
│  ├─ graphblocks-runtime
│  ├─ graphblocks-stdlib
│  ├─ graphblocks-documents
│  ├─ graphblocks-rag
│  ├─ graphblocks-conversation
│  ├─ graphblocks-policy
│  ├─ graphblocks-budget
│  ├─ graphblocks-usage
│  └─ optional packages
├─ schemas/
├─ tck/
├─ acceptance/
├─ deployment/
└─ docs/
```

Rust crate 이름은 명세의 권장 workspace 이름을 기준으로 하며, v1에서는 `graphblocks-*` 형식을 canonical name으로 사용한다. `gb-schema`, `gb-compiler`, `gb-runtime-core`, `gb-runtime-seq`, `gb-python` 같은 `gb-*` 이름은 논의용 약칭일 뿐이고, 별도 rename 결정 전까지 crate name이나 release artifact name으로 사용하지 않는다.

이 계획에서 `core`라는 단어는 두 의미로만 사용한다. `graphblocks-core`는 Python authoring/schema 배포 패키지이고 Rust crate가 아니다. Rust runtime core는 `graphblocks-runtime-core` crate이며, `graphblocks-runtime`은 Python runtime wheel/source package 이름이다.

Compiler authority는 Rust에 있다. `graphblocks-compiler` Rust crate가 normalized IR, canonical serialization, plan hashing의 normative reference implementation이다. Python `graphblocks-core`는 authoring/schema facade이며, 독립 validation 구현을 제공할 경우 Rust compiler와 동일한 TCK 결과 및 canonical hash를 생성해야 한다.

Rust crate는 `graphblocks-python`을 제외하고 PyO3에 의존하지 않는다. binding 구현은 하나만 둔다.

- `crates/graphblocks-python/`: 실제 PyO3 crate와 async bridge를 소유한다.
- `packages/graphblocks-runtime/`: `pyproject.toml`, Python wrapper/stub, packaging metadata를 가진다. Cargo manifest가 필요할 경우 workspace의 `crates/graphblocks-python`을 참조하고, 별도의 두 번째 binding 구현은 두지 않는다.

## 3. Phase 0 — Contract Toolchain (`GB-C0-SCHEMA`)

### 구현

- canonical schema registry와 schema ID/version 규칙
- GraphSpec/ApplicationSpec/BindingSpec parse, validation, normalization
- normalized IR canonical serialization과 content hash
- BlockDescriptor, typed port, resource slot, implementation manifest
- migration reader: v1alpha1/v1alpha2 → v1alpha3
- Python Pydantic/type stub과 Rust serde type 동등성 검사
- `graphblocks validate`, `plan`, `migrate`, `plugins list`
- schema/compiler TCK harness

### 첫 package

```text
Rust crates:
graphblocks-schema
graphblocks-types
graphblocks-compiler

Python distributions:
graphblocks-core
graphblocks-stdlib
graphblocks-testing
graphblocks-cli
```

### 종료 기준

- 동일 입력은 플랫폼과 map ordering에 관계없이 동일 normalized hash를 만든다.
- canonical value JSON round trip이 Rust/Python에서 동일하다.
- Python `graphblocks-core`와 Rust `graphblocks-compiler`가 같은 TCK 결과와 canonical plan hash를 만든다.
- port mismatch, dead node, optional-output misuse, ambiguous binding을 compile 시 탐지한다.
- plugin manifest를 import 없이 탐색하고 충돌을 결정론적으로 거부한다.

### 현재 진행

- Schema ID parsing is covered by the shared `tck/schema/cases.json` fixture on the Rust
  `graphblocks-schema` side.
- Typed value schema envelopes now use the shared `tck/schema/typed-values.json` fixture from both
  Python `graphblocks-core` and Rust `graphblocks-types`; Rust exposes canonical-value and
  canonical-JSON helpers backed by the normative compiler canonicalizer so Python/Rust parity is
  asserted on the same cases.
- `graphblocks-testing` can load and run the typed-value schema fixture through the shared schema
  TCK runner, so downstream conformance tooling can exercise the same Python contract instead of
  relying on package-local tests only. TCK suite manifests now surface auxiliary suite fixtures such
  as `schema/typed-values.json` alongside the primary `cases.json`.
- Package lock generation now validates selected dependency closures against each package's
  `forbiddenDependencies`, so optional integration SDKs cannot enter a generated lock indirectly
  through a transitive dependency. `graphblocks packages doctor` reports direct and transitive
  forbidden dependency selections as catalog diagnostics. Default metapackage lock generation also
  rejects packages in categories listed by `excludedCategories`, while still allowing those
  integrations when explicitly requested outside the default closure; the doctor reports the same
  excluded-default closure issue as a catalog diagnostic.
- Package manifest audit now normalizes PEP 508 direct-reference dependencies before applying
  blocked dependency policy, so `name @ URL` and optional extra direct references cannot bypass the
  license/vulnerability gate.

## 4. Phase 1 — Local Rust Runtime (`GB-C1-LOCAL-RUNTIME`)

### 구현

- Tokio scheduler와 dependency readiness
- typed receive/send port와 bounded channel
- `Outcome<T>` terminal model
- structured cancellation과 resource scope
- timeout, retry, idempotency boundary
- local semaphore/rate limit/lease
- RunStore와 ExecutionJournal의 in-memory/SQLite reference backend
- state patch와 CAS
- finite sequence map/batch/task group
- Python binding과 Python in-process/worker adapter
- deterministic InProcessTestRuntime

### 첫 vertical slice

```text
Message
→ prompt.render
→ scripted model.generate
→ Answer
→ conversation.begin/commit
→ ExecutionJournal
```

### 종료 기준

- single terminal, cancel idempotency, no-output-after-terminal TCK 통과
- partial output 후 unsafe retry를 거부한다.
- process shutdown 시 lease와 task가 남지 않는다.
- Python callback이 존재해도 scheduler ownership은 Rust에 남는다.

## 4.1 Amendment — Tool Execution and Policy-Governed Output Streaming (`GB-C1-TOOLS-OUTPUT`)

이 amendment는 tool execution과 streaming output policy를 prompt, model behavior, application callback, optional graph node에 맡기지 않고 runtime semantics로 구현한다. Rust runtime 또는 우회 불가능한 trusted runtime adapter가 mandatory policy enforcement point를 소유한다.

### 구현

- `ToolDefinition`은 model-visible contract만 포함한다. credentials, transport config, provider SDK object, mutable implementation detail은 포함하지 않는다.
- `ToolBinding`과 `ToolImplementation`은 block, graph, remote service, MCP server, OpenAPI operation 실행 방식을 분리해서 소유한다.
- model invocation 전에 application/graph/principal/tenant/conversation/data-classification/deployment/budget intersection으로 `ResolvedTool` set을 생성하고 run provenance에 기록한다.
- `ToolCallDraft`는 streaming argument fragment만 표현하며 side effect를 실행할 수 없다.
- final `ToolCall`은 schema-valid immutable arguments와 `arguments_digest`를 가진다. argument mutation은 revision과 approval을 invalidation한다.
- tool admission sequence는 resolve, JSON parse, input schema validation, `before_tool_or_effect` policy, budget/resource permit, approval, sandbox/target allocation, idempotency key, effect precondition, execution, result validation/redaction, usage/effect outcome 기록 순서로 고정한다.
- `ToolResult`는 final durable result이고 incremental tool output은 draft projection으로만 취급한다.
- `ToolExecutionPlan`은 parallelism, dependency failure policy, cancellation policy, effect serialization key를 명시한다. conflicting state-changing effects는 concurrently 실행하지 않는다.
- `PolicyRequest.enforcement_point`에 `on_generation_chunk`, `before_client_delivery`, `before_output_commit`, `before_tool_or_effect`를 추가한다.
- `OutputPolicyDecision`, `OutputDeliveryPolicy`, `OutputCutoff` schema와 terminal semantics를 canonical contract로 추가한다.
- output delivery path는 `GenerationChunk` normalization → `on_generation_chunk` policy evaluation → policy holdback buffer → `before_client_delivery` → `ApplicationEventStream` → client 순서를 따른다.
- `buffer_until_commit`, `bounded_holdback`, `immediate_draft` delivery mode를 지원한다. policy-sensitive streaming의 recommended default는 `bounded_holdback`이다.
- `abort_response`는 local delivery cutoff를 즉시 수행하고 provider/worker cancellation은 cooperative request로 처리한다. local cutoff가 authoritative하다.
- policy-aborted response는 assistant message나 tool result를 durable commit하지 않는다. safe replacement는 새 `response_id`를 사용한다.
- pending tool call draft는 model output이므로 output policy pipeline을 통과해야 하며, aborted response의 non-admitted call은 denied 상태가 된다.
- standard application events에 tool lifecycle events, output policy evaluation events, `OutputCutoff`, `AssistantIncomplete`, `AssistantRetracted`를 추가한다.

### Package ownership

- `graphblocks-core`: Python authoring/schema facade for `ToolDefinition`, `ToolCall`, `ToolResult`, `OutputPolicyDecision`, `OutputCutoff` schemas.
- `graphblocks-runtime-core`: lifecycle state machines, policy holdback buffer, mandatory delivery cutoff, terminal-state enforcement, cancellation propagation.
- `graphblocks-runtime-seq` and `graphblocks-runtime-durable`: sequential/durable execution of admitted tool calls, effect serialization, replay-safe terminal state.
- `graphblocks-policy`: canonical policy requests, decisions, obligations, output-policy evaluator contract.
- `graphblocks-agents`: `tools.resolve`, `agent.run`, `ToolExecutionPlan` orchestration semantics.
- `graphblocks-mcp`: MCP tool adapter.
- `graphblocks-openapi`: OpenAPI operation adapter.
- `graphblocks-policy-opa` and `graphblocks-policy-cedar`: optional external PDP adapters.

### 종료 기준

- A tool cannot execute before arguments are complete and schema-valid.
- Model output alone never authorizes a tool effect.
- Approval is bound to immutable tool-call revision, definition digest, binding digest, argument digest, policy snapshot, principal, and expiration.
- Retried state-changing tools preserve idempotency keys unless policy creates a new logical operation.
- Tool output passes result validation and content policy before it is returned to a model.
- Output policy enforcement occurs before mandatory client delivery.
- Once a response reaches `POLICY_STOPPED`, no later delta can be delivered or committed.
- Provider cancellation is cooperative; local delivery cutoff is authoritative.
- Already-delivered draft is represented with `keep`, `mark_incomplete`, or `retract` semantics.
- Aborted responses do not commit assistant messages.
- Pending tool calls belonging to aborted responses are not admitted.
- Running effects preserve atomicity, idempotency, audit, and compensation guarantees during cancellation.
- Late provider/tool usage is reconciled in `UsageLedger`.
- Mandatory policy enforcement cannot be bypassed by omitting a graph node.

### Compiler and conformance diagnostics

```text
ToolBindingMissing
ToolSchemaMissing
ApprovalWithoutArgumentDigest
UnsafeParallelEffects
NonIdempotentRetry
OutputPolicyBypass
ImmediateDraftWithoutRetractionSupport
PolicyGateAfterDelivery
PendingToolCallAfterAbort
CommitAfterPolicyStop
UnboundedPolicyHoldback
```

Required TCK coverage includes incremental arguments not triggering execution, invalid arguments denied before admission, approval invalidated after argument mutation, independent reads running concurrently, conflicting writes serialized, policy abort denying pending tool calls, local delivery cutoff before late provider chunks, delayed chunks discarded after `OutputCutoff`, immediate draft producing incomplete/retracted events, `buffer_until_commit` exposing no rejected content, aborted responses not committing assistant messages, late usage reconciliation, and idempotency preservation across retry/cancellation.

## 4.2 Amendment — Durable Async Runs and Callback Protocol (`GB-C1-ASYNC-CALLBACKS`)

This amendment makes long-running GraphBlocks runs independent from any single client connection.
`ApplicationEventStream` is the authoritative replayable stream; callback subscriptions are delivery
projections; external callbacks are authenticated resume signals for `AsyncOperation`.

### 구현

- Add run invocation modes `sync`, `accepted`, and `background`. `accepted` and `background` return a
  run handle immediately and persist cursor-replayable events.
- Extend run lifecycle with `WAITING_CALLBACK`, `PAUSED_BUDGET`, `PAUSED_CALLBACK_DELIVERY`,
  `PAUSED_POLICY`, `PAUSED_OPERATOR`, and `RESUMING`.
- Add application protocol commands: `GetRunStatus`, `ListRuns`, `AttachToRun`, `DetachFromRun`,
  `SubscribeEvents`, `UnsubscribeEvents`, `AckEvent`, `RegisterCallback`, `RevokeCallback`,
  `SubmitAsyncCallback`, `PauseRun`, `ResumeRun`, `ExpireRun`, `RedriveCallbackDelivery`, and
  `MoveCallbackToDeadLetter`.
- Add schema/runtime models for `CallbackSubscription`, `EventFilter`, callback delivery targets,
  `CallbackDelivery`, `CallbackEnvelope`, `AsyncOperation`, `CallbackEndpointRef`,
  `ExternalCallbackReceived`, and `AsyncOperationResult`.
- Add standard blocks: `async.start_operation`, `async.await_callback`, `async.poll_operation`,
  `async.complete_operation`, `async.cancel_operation`, and `async.expire_operation`.
- Callback ingestion must authenticate, check ownership and attempt fencing, validate idempotency
  and schema, evaluate policy, journal `ExternalCallbackReceived`, update operation state, and only
  then signal resume.
- Callback delivery retry uses bounded exponential backoff with jitter and dead-letter preservation.
  Exactly-once delivery is not promised. Subscription and callback registration projections validate
  event filter shape and spec failure policy literals before storage.
- Resume from callback re-evaluates policy, budget, release compatibility, ownership lease,
  worker availability, callback authenticity, and idempotency state.
- Large callback payloads are rejected or converted to `ArtifactRef`; callback payloads are always
  untrusted content.
- Callback payload projections require a stored digest. Inline projections validate that digest
  and canonical byte count match the JSON payload. Artifact-backed projections keep the digest and
  size of the original oversized payload while carrying only an `ArtifactRef` inline.
- A duplicate external callback is idempotent only when the reused idempotency key points to the
  same logical callback receipt. Reusing an idempotency key with a different operation identity,
  attempt, provider operation, payload digest, verification principal, or policy snapshot is an
  `idempotency_conflict` rejection and MUST NOT overwrite the original receipt or resume the run.
- `AsyncOperationResult` records final async operation status, output projections, artifacts,
  diagnostics, metrics, checks, usage, and external effect records. Cancellation or timeout after an
  external provider committed a side effect MUST preserve that committed effect in the result
  projection and downstream audit/ledger path; cancellation is not treated as rollback.

Example duplicate callback handling:

```text
first callback:
  operation_id = op-1
  idempotency_key = provider-delivery-1
  payload_digest = sha256:aaa
  result = ExternalCallbackReceived, resume eligible

exact duplicate:
  operation_id = op-1
  idempotency_key = provider-delivery-1
  payload_digest = sha256:aaa
  result = duplicate acknowledgement, no second resume

conflicting replay:
  operation_id = op-1
  idempotency_key = provider-delivery-1
  payload_digest = sha256:bbb
  result = ExternalCallbackRejected(idempotency_conflict:payload_digest), no overwrite, no resume
```

Example callback-before-operation-commit handling:

```text
provider responds before AsyncOperation commit is visible:
  result = callback is authenticated and quarantined under (operation_id, idempotency_key)

operation commit completes:
  result = quarantined callback is replayed through normal callback admission

after replay:
  result = ExternalCallbackReceived is journaled before resume, quarantine entry is removed
```

Example cancelled operation with committed external effect:

```yaml
nodes:
  cancelTicketWrite:
    block: async.cancel_operation
    in:
      operation: startTicketWrite.operation
    config:
      cancelledAtUnixMs: 1900
      externalEffects:
        - effectId: effect-ticket-1
          target: ticket-system
          operation: ticket.create
          outcome: committed
          idempotencyKey: idem-ticket-1
          providerEffectId: ticket-123
```

Example Codex-like background coding agent:

```yaml
application:
  id: workspace-coding-agent
  capabilities:
    - background_runs
    - cursor_replay
    - callback_subscription
    - reconnect_resume
  routes:
    - id: create-task
      command: InvokeGraph
      responseMode: accepted
    - id: run-events
      transport: sse
      cursorReplay: true
    - id: external-callback
      command: SubmitAsyncCallback
```

Full example: `examples/11-coding-agent-background-callbacks.yaml`.

### Current implementation slice

- `graphblocks-runtime-core::run_store::RunStatus` and the Python run-store facade now include the
  durable async lifecycle states `admitted`, `waiting_input`, `waiting_approval`, `waiting_review`,
  `waiting_callback`, `paused_budget`, `paused_callback_delivery`, `paused_policy`,
  `paused_operator`, `resuming`, and terminal `completed` and `expired`; SQLite persistence tests
  cover the new state strings.
- `RunInvocationMode` now records `sync`, `accepted`, and `background` invocation mode in
  `RunRecord`; the Python run-store facade validates and persists the mode through SQLite migration,
  and the server builds accepted/background run handles with event stream, websocket, cancel route,
  and initial cursor fields.
- Run invocation route diagnostics now report accepted/background routes without cursor-replayable
  event streams as `GB6005`, with shared compiler TCK coverage.
- Run invocation route diagnostics now report accepted/background routes tied to
  `client_connection` lifetime as `GB6009`, with shared compiler TCK coverage.
- Run invocation route diagnostics now compare declared event retention to reconnect/replay
  guarantees and report insufficient retention as `GB6013`, with shared compiler TCK coverage.
- Run status snapshots now expose the protocol response shape with state, release id, last cursor,
  started/updated/completed timestamps, wait reasons, and active async operation ids.
- `RunOwnershipLease` now provides run-scoped coordinator ownership fencing in both in-memory and
  SQLite run stores, including active-lease rejection, stale epoch rejection, and failover after
  expiry.
- Run state and status mutations now have lease-fenced APIs in both in-memory and SQLite run stores;
  stale coordinators cannot patch run state or advance status after failover, and SQLite validates
  the lease and mutation in one transaction.
- `ApplicationCommandKind` now includes the async run, attach/replay, subscription, callback
  registration, callback ingestion, pause/resume/expire, redrive, and dead-letter command names
  from the amendment.
- `graphblocks-runtime-core::async_operation` now contains the in-memory `AsyncOperation` and
  callback ingestion state machine for the first TDD slice.
- Implemented behavior covers operation registration, submitted-to-waiting journal entries,
  schema-validated `ExternalCallbackReceived` records, idempotent duplicate callback handling,
  stale-attempt rejection, terminal expiration/cancellation transitions, diagnostic late callback
  records after terminal states, and the required journal-before-resume ordering.
- Focused tests include duplicate delivery, invalid callback schema, stale attempt fencing,
  callback-after-timeout/cancellation, concurrent duplicate callback racing, callback/cancel racing,
  whitespace-only operation registration and callback identity rejection at endpoint and store
  boundaries, and a deterministic fuzz-style idempotency sequence.
- Callback ingestion now enforces the specification's default `262144` byte payload limit before
  journaling or resume, and focused tests cover explicit small-limit rejection without operation
  state changes.
- Callback ingestion can also accept oversized callback payloads as artifact-backed receipts when
  the caller supplies a `CallbackArtifactRef`; the runtime journals only compact callback metadata
  plus the artifact reference, and SQLite persistence preserves the artifact-backed receipt across
  reopen.
- Audit helpers now produce metadata-only audit events for `ExternalCallbackReceived` and
  `ExternalCallbackRejected`, recording operation/run/attempt identity, policy snapshot, release,
  idempotency, verification, payload digest, and artifact ids without copying untrusted callback
  payload bodies into the audit log.
- Observability now exposes typed names for the amendment's required async operation, callback
  delivery, and run attach/detach/replay events, and `ObservabilityObservation` validates metric
  labels against the low-cardinality rule including `operation_id`, `event_id`, and `delivery_id`.
- `CallbackEndpointRef` and `CallbackEndpointAuth` now model callback ingress authentication for
  async operations, with bearer-token, `hmac-sha256`, Ed25519 verifier-boundary, mTLS
  client-identity, and OIDC/JWT verifier-boundary helpers that build `AsyncCallbackSubmission` only
  after authentication succeeds.
- `CallbackEndpointRef` now validates `expires_at` as an ISO-8601 timestamp at creation time, so
  invalid callback endpoint deadlines are rejected before resume admission.
- Callback rejection paths now emit durable `ExternalCallbackRejected` metadata events for stale
  attempts, schema mismatches, and payload-limit failures without journaling rejected payload bodies;
  SQLite persistence covers these rejection events across reopen.
- Async operation configuration diagnostics now report missing callback timeout (`GB6001`), missing
  idempotency key (`GB6003`), and missing callback schema (`GB6007`) in deterministic order for
  top-level `asyncOperations` and `async.start_operation`/`async.await_callback` node configs, with
  shared compiler TCK coverage.
- Async operation configuration diagnostics now compare declared expected callback payload size to
  the configured ingestion limit and report oversized inline callback payloads as `GB6010`, with
  shared compiler TCK coverage.
- Async operation configuration diagnostics now report callback waits that can resume without
  policy, budget, and release-compatibility re-evaluation as `GB6008`, with shared compiler TCK
  coverage.
- Async operation configuration diagnostics now report callback waits without attempt fencing,
  where stale callbacks could resume newer attempts, as `GB6015`, with shared compiler TCK coverage.
- Async operation configuration diagnostics now report callback waits that can resume without run
  ownership lease or fencing protection as `GB6016`, with shared compiler TCK coverage.
- The normative `graphblocks-compiler` Rust crate now emits the same `GB6001` through `GB6016`
  async/callback diagnostics as the Python authoring facade and passes the shared compiler TCK for
  these cases.
- `SqliteAsyncOperationStore` now persists async operations, operation event journals, and external
  callback receipts across reopen, including idempotency-key duplicate detection after restart.
- Callback receipt duplicate detection is now scoped by `(operation_id, idempotency_key)` in both
  in-memory and SQLite async operation stores, so provider delivery keys reused by separate
  operations do not suppress valid callback receipts or resume signals.
- Callback receipt duplicate detection now rejects idempotency-key conflicts when a replay mutates
  callback identity or payload digest; in-memory, SQLite-reopen, and deterministic fuzz tests verify
  that the original receipt remains authoritative and no second resume is produced.
- Async callback ingestion now supports durable pre-operation quarantine for the race where an
  external provider replies before the committed `AsyncOperation` is visible. Quarantined callbacks
  are keyed by `(operation_id, idempotency_key)`, persist across SQLite reopen, deduplicate
  repeated provider delivery attempts, and are consumed through the normal journal-before-resume
  callback admission path after operation registration.
- Pre-operation quarantine conflict tests now cover mutated provider replays before operation
  registration, including deterministic fuzz-style sequences that prove the first quarantined
  submission remains authoritative across replay and durable SQLite reopen.
- Expired pre-operation quarantine entries are discarded instead of replayed after operation
  registration; the runtime emits `ExternalCallbackRejected` metadata with
  `quarantined_callback_expired`, leaves the operation in `WAITING_CALLBACK`, and does not produce
  a resume signal. In-memory and SQLite tests cover this edge case.
- Callback resume admission can now pause after a durable callback receipt when budget policy
  denies continuation; the operation records `CallbackReceived`, emits a pause reason, and returns
  `should_resume = false`.
- Callback resume admission also records policy-denied and release-incompatible resume decisions
  after durable callback receipt, preserving the journal-before-resume rule while preventing
  scheduler continuation.
- Python `graphblocks-core` now exposes an immutable `AsyncOperation` schema facade with the
  amendment states (`created`, `submitted`, `waiting_callback`, `callback_received`, `polling`,
  `resuming`, and terminal states), callback/polling refs, expected schema, resume token hash,
  idempotency key, timestamps, transition helpers, and JSON projection coverage.
- The Python `AsyncOperation` facade now enforces the amendment state machine: callbacks must move
  through `waiting_callback` before `callback_received`, polling must be explicit before terminal
  poll results, and terminal operations cannot transition again.
- The Python `AsyncOperation` facade now validates state/timestamp consistency: non-created states
  require `submitted_at`, terminal states require `completed_at`, and `created` records cannot
  already carry submitted or completed timestamps.
- The Python `AsyncOperation` facade now validates ISO datetime syntax and ordering for
  `created_at`, `submitted_at`, `completed_at`, and `expires_at`, including offset-aware comparisons
  for submitted-before-created, completed-before-submitted, non-positive expiry windows, and
  expiry deadlines that are already elapsed by submission time.
- The Python `AsyncOperation` facade now rejects provider operation identity before submission, so
  `provider_operation_id` cannot appear on a still-created operation record and provider invocation
  remains separated from durable operation creation.
- The Python `AsyncOperation` facade now enforces the amendment's bounded-wait invariant at runtime:
  callback and polling waits require either `expires_at` or an explicit `infinite_wait_policy`, with
  deterministic fuzz coverage for deadline/policy combinations.
- `AsyncOperationResult` and `ExternalEffectRecord` now preserve committed external side effects
  even when an async operation result is `cancelled` or `incomplete`; stdlib async terminal blocks
  can project `externalEffects` config into the final result instead of dropping provider effect
  identity.
- Python `graphblocks-core` now exposes the same authoring/schema facade for
  `AsyncOperationResult`, `AsyncOperationResultStatus`, and `ExternalEffectRecord`, including
  validation that provider effect identity is only attached to committed external effects.
- Python `AsyncOperationResult` now validates output, artifacts, diagnostics, metrics, checks, and
  usage projections as strict JSON-compatible values, deep-freezes them on construction, and
  returns thawed copies from `to_json()` so untrusted callback/result payloads cannot be mutated
  after journaling.
- Python `AsyncOperationResult.from_operation` now projects durable results only from terminal
  `AsyncOperation` records, mapping terminal state to result status while preserving the operation
  id and rejecting non-terminal waits or resumes.
- `graphblocks-runtime-core::stdlib_runtime` now exposes deterministic `async.start_operation@1`
  and `async.await_callback@1` blocks so graph-level examples can start an external operation and
  checkpoint while waiting for callback without treating callback delivery as the source of truth.
- The stdlib runtime also exposes `async.poll_operation@1`, `async.complete_operation@1`,
  `async.cancel_operation@1`, and `async.expire_operation@1` projections for polling and terminal
  async operation results.
- `graphblocks-runtime-core::callback_delivery` now contains callback subscription filtering,
  deterministic delivery records, idempotency keys, success/duplicate acknowledgement handling,
  bounded retry scheduling, best-effort failure handling, dead-letter terminal state, and redrive
  records that preserve original delivery identity, event identity, attempt history, operator, and
  reason.
- Callback delivery targets are now typed as webhook, WebSocket, SSE, push notification, email, or
  local callback variants, and ordered-delivery diagnostics use target capabilities instead of
  string-prefix inference.
- `graphblocks-server` now enforces callback delivery target safety at registration and subscription
  admission: webhook delivery requires HMAC-SHA256 or Ed25519 signing metadata and rejects obvious
  forbidden egress targets such as localhost, private/link-local IPs, `file://`, and Unix socket URLs.
- Callback event filters now include visibility, node ID, operation ID, and minimum severity
  predicates in addition to event type and terminal-event inclusion.
- `SqliteCallbackDeadLetterStore` now persists callback dead-letter records across reopen and can
  redrive them while preserving original delivery identity, idempotency key, attempt history, and
  audit-visible redrive count.
- Callback dead-letter records can now project an operator redrive back into a pending delivery
  without minting a new application event identity, preserving the original delivery, event,
  subscription, run, cursor, and idempotency identifiers while advancing the delivery attempt.
- Callback dead-letter records now reject inconsistent projections whose wrapped delivery is not
  `dead_lettered` or whose attempt history omits the dead-lettered delivery attempt.
- Mandatory callback failure policies now map terminal delivery failures to explicit runtime
  actions: pause the run for `pause_run_on_failure`, fail the run for `fail_run_on_failure`, and
  avoid run terminal actions for ordinary retry/dead-letter subscriptions.
- Ordered callback delivery now tracks the blocking delivery per subscription/run and prevents later
  events from scheduling until the prior delivery succeeds, is acknowledged, fails terminally,
  dead-letters, is cancelled, or expires.
- Webhook delivery envelopes now support required GraphBlocks headers, canonical JSON signing,
  `hmac-sha256` verification, replay-window enforcement, and header/body identity checks.
- Callback envelopes now validate `occurred_at` and `delivered_at` as ISO-8601 timestamps and
  reject deliveries whose delivery timestamp precedes the source event timestamp.
- External callback receipts now validate `received_at` as an ISO-8601 timestamp and reject receipt
  records whose durable receipt time precedes the callback envelope delivery time.
- `SqliteCallbackDeliveryQueue` now persists pending and retry-scheduled callback deliveries across
  reopen, preserving delivery status, idempotency keys, sequence ordering, and retry due times.
- `WebhookDeliveryWorker` now processes due durable callback deliveries with signed webhook
  envelopes, an injected transport boundary, and persisted success/retry outcomes.
- `CallbackDeliveryProjection` now exposes a response-transition helper that applies classified
  webhook receiver responses to durable delivery state: 2xx marks delivered, 409 marks acknowledged,
  429/5xx schedule bounded retries, and retry exhaustion remains failed without over-scheduling.
- Callback delivery response transitions now reject late receiver responses once a delivery is
  already terminal, preventing delivered, acknowledged, dead-lettered, cancelled, or expired
  delivery records from being rewritten by delayed network outcomes.
- Callback delivery projections now validate retry, delivery, and acknowledgement timestamps as
  ISO-8601 datetimes and reject acknowledgement timestamps that precede delivery timestamps.
- Callback subscriptions can schedule cursor replay from the authoritative `ApplicationProtocolLog`
  while applying the same event filters and deterministic delivery/idempotency metadata as live
  projection.
- `ApplicationProtocolLog` now exposes retained-window replay with explicit `CursorExpired`
  semantics, including the requested cursor, nearest retained cursor, last cursor, and last
  sequence for reconnect/attach callers.
- `AttachToRun` replay now has a typed runtime result that either returns retained missed events
  and the live-stream cursor, or reports expired-cursor recovery metadata.
- Webhook delivery targets now have default-deny endpoint validation for unsupported schemes,
  localhost, loopback, private RFC1918 ranges, link-local metadata addresses, and malformed hosts,
  with explicit host allowlisting for trusted development or private deployments.
- Callback configuration diagnostics now map unsigned webhook subscriptions to `GB6002` and unsafe
  webhook endpoint failures, including userinfo-bearing URLs, to `GB6011` for compiler/deployment
  reporting, with shared compiler TCK coverage.
- Callback subscriptions can now explicitly mark forbidden authoritative uses, and diagnostics
  report callback delivery used as a source of truth for run correctness, billing, quota, audit, or
  effect commit as `GB6004`, with shared compiler TCK coverage.
- Callback subscription diagnostics now report mandatory callback delivery without retry,
  dead-letter, or fallback policy as `GB6006`, with shared compiler TCK coverage.
- Callback subscription diagnostics now report impossible ordered-delivery requests (`GB6012`) and
  mandatory callback failure policies without dead-letter behavior (`GB6014`), with shared compiler
  TCK coverage.
- Webhook delivery targets now enforce the specification's default `262144` byte payload limit
  before signing delivery envelopes, and tests cover explicit small-limit rejection for oversized
  callback projections.
- Webhook egress policy now validates DNS-resolved addresses before transport, rejecting public
  hostnames that resolve to loopback, private, link-local, metadata, or otherwise forbidden
  addresses unless the host is explicitly allowlisted.
- `WebhookHttpTransport` now provides the runtime transport adapter boundary: DNS preflight,
  signed POST request construction, and spec-defined receiver status mapping are implemented
  without adding a default HTTP/TLS client dependency to `graphblocks-runtime-core`.
- Deployment manifests now include a `CallbackIngressConfig` contract for async callback ingress
  routes, signature and anti-enumeration security, payload/rate limits, stable digests, and `GB6002`
  diagnostics when enabled callback ingress does not require signatures.
- `graphblocks-kubernetes` now renders callback ingress manifest sets as a Service, Gateway API
  `HTTPRoute`, and ingress-only `NetworkPolicy`, preserving signature, anti-enumeration, payload,
  and rate-limit metadata without mutating any live cluster.
- `graphblocks-server` now exposes the framework-neutral `POST /callbacks/{operation_id}`
  `SubmitAsyncCallback` route and typed `ServerAsyncCallbackSubmission` contract, accepting
  authenticated callback ingress signals with idempotency keys, acknowledging duplicate callback
  submissions without recording them twice, and rejecting conflicting replays that reuse an
  idempotency key with different content while leaving durable journal/resume authority to the
  runtime. Callback submissions that declare a `run_id` must reference a retained run event stream
  and include an `attempt_id` fence before they are accepted; once an operation has an accepted
  run-attempt receipt, later callbacks for that operation cannot switch to a different run attempt.
  Callback ingress rejects run-scoped receipts when the authoritative run projection is already
  terminal, so late callbacks cannot appear resumable or create new stored resume receipts.
  The server route enforces a configurable inline callback payload limit, defaulting to the
  specification's `262144` bytes, before accepting or storing a callback receipt.
  Callback receipt timestamps are validated as ISO datetimes, and nested callback JSON payloads are
  deep-frozen at ingress so later caller mutation cannot corrupt stored callback receipts or
  idempotency comparisons.
- `graphblocks-server` now also exposes the framework-neutral `GET /runs/{run_id}`
  `GetRunStatus` route, deriving status, release id, replay cursor, timestamps, wait reasons, and
  active operation projection from the authoritative stored application events and accepted async
  callback submissions. Terminal run states suppress active callback wait projections so late
  callback receipts do not appear resumable after cancellation, expiry, failure, or policy stop.
  Run-control pauses project operator, budget, policy, and callback-delivery wait reasons.
- Stored server application events are immutable snapshots; `/events`, attach/replay, subscription
  replay, and websocket snapshot responses thaw them back to plain JSON payloads.
- `graphblocks-server` now exposes the framework-neutral `GET /runs` `ListRuns` route using the
  same event-derived run status projection, keeping `POST /runs` reserved for `InvokeGraph`.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/attach`
  `AttachToRun` route, replaying stored events after a supplied cursor and returning explicit
  `CursorExpired` recovery metadata when the requested cursor is no longer retained. Attach cursors
  must belong to the target run; wrong-run cursors are rejected before retention lookup.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/detach`
  `DetachFromRun` route, recording client detach projections while preserving the authoritative
  event stream and current run status. Stored detach projection records are immutable snapshots,
  and detach timestamps are validated as ISO datetimes. Repeated detach requests from the same
  client are idempotent and return the first detach record.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/subscriptions`
  `SubscribeEvents` route, recording run-scoped event subscription projections and replaying
  retained matching events from the authoritative event stream after an optional cursor. Replay
  filters honor event type, visibility, node ID, operation ID, minimum severity, and
  `includeTerminalEvents` predicates. Visibility, node, and operation filters match the
  specification's top-level `visibility`, `nodeId`, and `operationId` event fields and legacy
  payload fields. Visibility filters validate the specification's `client`, `operator`,
  `internal`, and `audit_only` literals. Nested event filter and delivery configs are immutable
  snapshots and are thawed back to plain JSON for response payloads.
  Run-scoped subscription ids are single-assignment and cannot overwrite an existing active or
  revoked projection. Subscription replay cursors must belong to the subscribed run before retention
  lookup. Subscription and callback registration projections validate the spec failure policy
  literals before storage, and ordered delivery requests are rejected unless the target kind can
  preserve run ordering. Mandatory delivery projections cannot use best-effort failure handling
  unless an explicit dead-letter configuration is supplied. Route validation rejects callback
  delivery projections that mark themselves as a source of truth. Subscription creation timestamps
  are validated as ISO datetimes before storage.
- `graphblocks-server` now exposes the framework-neutral
  `DELETE /runs/{run_id}/subscriptions/{subscription_id}` `UnsubscribeEvents` route, revoking
  subscription projections without deleting the authoritative event stream. Revoked subscriptions
  cannot accept new `AckEvent` records, and repeated unsubscribe requests are idempotent.
- `graphblocks-server` now exposes the framework-neutral
  `POST /runs/{run_id}/subscriptions/{subscription_id}/ack` `AckEvent` route, recording event
  acknowledgements by event id or cursor without mutating the authoritative event stream. Stored
  acknowledgement projection records are immutable snapshots, and repeated acknowledgements for the
  same event/cursor return an explicit duplicate acknowledgement with the first acknowledgement
  timestamp. Acknowledgement timestamps are validated as ISO datetimes. When a request supplies
  both event id and cursor, both identifiers must resolve to the same retained event before an
  acknowledgement is recorded. Acknowledged events must also match the active subscription's event
  filter.
- `graphblocks-server` now exposes framework-neutral `POST /callbacks/register` and
  `DELETE /callbacks/{subscription_id}` `RegisterCallback`/`RevokeCallback` routes, storing
  callback delivery registration projections and replaying retained run-scoped matching events
  without making callback delivery authoritative. Callback registration validates the specification
  scope literals (`run`, `conversation`, `project`, `tenant`, `deployment`). Nested event filter and
  delivery configs are immutable snapshots and are thawed back to plain JSON for response payloads.
  Callback registration ids are single-assignment and cannot overwrite an existing active or
  revoked projection. Repeating `RevokeCallback` for an already revoked registration is idempotent
  and does not rewrite the stored projection. Callback registrations share the same route-level
  ordered delivery, mandatory failure-policy, non-authoritative projection, and creation timestamp
  validation as run-scoped subscriptions.
- `graphblocks-server` now exposes framework-neutral `POST /runs/{run_id}/cancel`,
  `POST /runs/{run_id}/pause`, `POST /runs/{run_id}/resume`, and
  `POST /runs/{run_id}/expire` `CancelRun`/`PauseRun`/`ResumeRun`/`ExpireRun` routes,
  recording run-control projections and reflecting the latest control state in `GetRunStatus`
  while preserving the authoritative event stream. `CancelRun` projects terminal `cancelled`, and
  both cancelled and expired controls set `completedAt` in status snapshots. Stored run-control
  projection records are immutable snapshots with ISO-validated timestamps, and `PauseRun` accepts
  `pauseKind` values `operator`, `budget`, `policy`, and `callback_delivery` to project the
  corresponding wait reason.
  Non-terminal controls cannot reopen terminal runs. Repeating the latest control state, including
  non-terminal pause/resume projections, is idempotent and does not append another projection.
- `graphblocks-server` `InvokeGraph` now honors `responseMode: accepted` and `background` by
  returning a durable run handle with event stream, `/ws` websocket, cancel route, and initial
  cursor while retaining authoritative run events for later attach/replay from that cursor.
  `InvokeGraph` validates event `occurredAt` timestamps as ISO datetimes before storing run events.
- `SubscribeEvents` and `RegisterCallback` server projections now have coverage for replay from
  the accepted/background run handle's initial cursor, so event subscriptions and callback
  registrations can attach from the beginning without treating the initial cursor as expired.
- `graphblocks-callbacks` is now cataloged as an optional pure-Python callback projection package
  with no default HTTP/WebSocket client dependency. Its initial facade projects webhook envelopes,
  required headers, and HMAC-SHA256 signing/verification helpers while keeping callback delivery
  non-authoritative relative to the event stream and runtime journals.
- The callback projection facade now validates webhook payloads as strict JSON before signing:
  object keys must be strings, non-finite numbers are rejected, payloads are deep-copied, and a
  deterministic fuzz-style test pins signature stability under key reordering and caller mutation.
- `graphblocks-callbacks` also exposes receiver-side HMAC-SHA256 header verification with required
  GraphBlocks webhook header checks, envelope identity checks, and replay-window enforcement for
  local tools, tests, and embedded receivers.
- `graphblocks-callbacks` now includes dependency-free retry/dead-letter projection helpers:
  bounded deterministic jittered backoff, immutable delivery projections, dead-letter conversion,
  and redrive records that preserve original delivery identity, idempotency key, and attempt
  history without creating application events.
- `graphblocks-callbacks` now exposes a dependency-free webhook target safety helper for callback
  delivery adapters, rejecting unsupported schemes, userinfo URLs, localhost/metadata hosts, and
  loopback/private/link-local/reserved IP destinations unless private targets are explicitly
  allowed by deployment policy.
- `graphblocks-callbacks` now provides callback payload projection helpers that canonicalize
  strict JSON payloads, keep bounded payloads inline with a digest, and require an `ArtifactRef`
  when payloads exceed the configured inline byte limit.
- `graphblocks-callbacks` now maps webhook receiver HTTP responses into delivery decisions:
  2xx delivered, 409 acknowledged duplicate, 410 gone, 429/5xx retry, and other 4xx terminal
  failure, including `Retry-After` parsing for retry scheduling.
- `graphblocks-callbacks` HMAC helpers now support optional `GraphBlocks-Key-Id` emission and
  keyring verification so receivers can accept current and previous signing secrets during
  rotation while rejecting unknown key IDs.
- `graphblocks-callbacks` now includes an in-memory receiver replay guard that records callback
  delivery/idempotency identity, accepts first deliveries, treats exact repeats as duplicates, and
  flags mutated idempotency-key reuse as a conflict.
- `graphblocks-callbacks` now projects durable `ExternalCallbackReceived` receipt metadata from a
  verified callback envelope and bounded/artifact-backed payload projection, preserving callback,
  run, operation, node, attempt, idempotency, payload digest, verifier, and policy snapshot identity
  for journal-before-resume flows without making callback delivery the source of truth.
- `graphblocks-callbacks` now exposes callback endpoint auth/reference projections for bearer,
  HMAC, mTLS, and OIDC callback ingress. Endpoint refs bind accepted schema, operation, run, node,
  attempt, release, and tenant identity into a stable fencing key so stale callbacks cannot be
  confused with the current resumable operation.
- `graphblocks-callbacks` now evaluates callback resume admission by comparing a durable
  `ExternalCallbackReceived` receipt against the callback endpoint's tenant/release/run/node/
  attempt/operation fencing key and endpoint expiry, returning explicit admitted, expired, or stale
  decisions before any scheduler resume signal is represented.
- Callback resume admission has deterministic fuzz coverage over tenant, release, run, node,
  attempt, and operation identity mutations to protect the async callback path from stale-attempt
  and wrong-scope resume regressions.
- `graphblocks-server` now exposes framework-neutral
  `POST /callbacks/deliveries/{delivery_id}/redrive` and
  `POST /callbacks/deliveries/{delivery_id}/dead-letter`
  `RedriveCallbackDelivery`/`MoveCallbackToDeadLetter` routes, recording operator and reason
  projections while leaving durable callback queue/dead-letter authority in the runtime layer.
  Stored control projections are immutable snapshots with ISO-validated request timestamps, so
  inspection callers cannot mutate redrive or dead-letter history after recording. Repeated
  dead-letter moves for the same delivery are idempotent and return the first terminal move;
  redrive requests remain repeatable operator actions.
- `graphblocksd` now includes a server-side webhook HTTP client adapter that consumes the
  `graphblocks-runtime-core` signed webhook request, sends the exact canonical JSON body used for
  signing, maps response status and `Retry-After` back into the runtime delivery response model,
  and keeps the actual network client behind a daemon boundary instead of adding an HTTP/TLS client
  dependency to `graphblocks-runtime-core`. A TLS-capable production client can implement the same
  daemon adapter boundary.
- `examples/11-coding-agent-background-callbacks.yaml` now documents a concrete background coding
  agent application with accepted invocation, cursor replay, callback subscription, async CI
  operation start/wait, review, and CAS workspace commit.
- The coding-agent background callback example is now covered by a documentation contract test
  that loads its multi-document YAML and pins accepted invocation, SSE cursor replay, callback
  ingress, pre-commit quarantine, await-callback checkpointing, CAS commit, and signed webhook
  subscription semantics.

### Package ownership

- `graphblocks-core`: schema facades for events, callback subscriptions, deliveries,
  async operations, and external callback receipts.
- `graphblocks-runtime-core`: run lifecycle, event replay state, async operation state machine,
  callback ingestion, journal-before-resume, idempotency, stale-attempt rejection.
- `graphblocks-runtime-durable`: durable run store, cursor retention, coordinator failover,
  ownership fencing, checkpoint/resume.
- `graphblocks-callbacks`: optional webhook delivery, signing, retry, dead-letter, redrive, and
  receiver verification helpers.
- `graphblocks-server`: callback ingress routes, run event routes, SSE/WebSocket attach.
- `graphblocks-policy` and `graphblocks-budget`: callback/resume policy and pause/resume budget
  semantics.

### Compiler diagnostics

Add `GB6001` through `GB6016`: async wait without timeout, callback without authentication,
missing idempotency key, callback as source of truth, background run without replay,
mandatory callback without failure policy, missing callback schema, resume without policy
re-evaluation, client-bound background run, oversized callback payload, unsafe callback endpoint,
impossible ordering request, insufficient retention, missing dead-letter policy, stale callback can
resume, and resume without ownership fencing.

### Required conformance

- background run continues after client disconnect.
- client attaches with cursor and receives missed events.
- expired cursor returns `CursorExpired` and current summary/status.
- webhook delivery retries after 5xx and deduplicates by idempotency key.
- webhook 409 duplicate can mark delivery acknowledged.
- callback signature/schema failure does not resume run.
- callback after timeout, cancellation, or newer retry attempt does not resume stale work.
- callback receipt is journaled before resume.
- coordinator failover resumes once.
- budget exhaustion during wait records callback but pauses resume.
- policy/release compatibility is re-evaluated on resume.
- mandatory callback failure pauses or dead-letters according to policy.
- large callback payload is rejected or converted to `ArtifactRef`.
- dead-letter redrive does not create duplicate `ApplicationEvent`.
- non-mandatory webhook outage does not block run completion.
- external operation side-effect commit is preserved after cancellation.

## 5. Phase 2 — AI Core Profiles (`GB-C2-AI-APPLICATION`)

### Documents

- Artifact/SourceAsset/Revision/ParsedDocument/Element/Chunk
- MIME routing과 parser plugin SPI
- deterministic parser selection lock
- OCR fallback interface
- lineage, manifest, delete/tombstone
- BlobStore local/S3-compatible reference adapter

### RAG

- Retriever/SearchRequest/SearchHit/KnowledgeItemRef
- dense/keyword federation and RRF
- reranker SPI
- ContextPack budget and provenance
- Answer/Claim/Citation validation and abstention

### Conversation

- Conversation/Turn/Message/ContentPart
- begin/abort/commit transaction
- draft delta, committed, retracted
- attachment scope and memory compaction policy
- Application command/event protocol

### Acceptance applications

- direct multi-format file analysis
- incremental document ingestion
- federated enterprise RAG
- multi-turn attachment chatbot

### 종료 기준

- source location이 file→element→chunk→retrieval→claim→citation으로 resolve된다.
- ACL이 chunk/index/retrieval/citation에 전파된다.
- branch에서 `Absent`와 `Value(null)`이 구분된다.
- conversation CAS conflict와 regenerate/branch가 결정론적으로 처리된다.

### Current implementation slice

- `graphblocks-core`/`graphblocks-documents` now expose document lineage primitives, parser
  selection locks, local/S3-compatible blob adapters, and ingestion manifests with ACL-gated
  publish records. S3-compatible metadata normalization rejects case-colliding user metadata keys
  before artifact provenance can be silently overwritten.
- `InMemoryIngestionManifestStore` now distinguishes tombstone retention from hard delete via a
  typed `IngestionDeletePolicy`: tombstone retains a deleted manifest snapshot while hard delete
  removes the manifest and clears the current-asset pointer.
- RAG primitives cover local chunk indexing, tombstone/hard delete propagation, context packs,
  citation/source-trace resolution with retrieval rank/score/metadata provenance, answer grounding,
  abstention, fusion, and rerank projections.
- Conversation primitives cover CAS, tombstone/hard delete retention, branch/regenerate lineage,
  turn lifecycle, draft/retract semantics, and deterministic conflict handling.

## 6. Phase 3 — Policy, Usage, Budget, Evaluation (`GB-C3-GOVERNED-RUNTIME`)

### 구현

- PolicyBundle/Profile/Snapshot, PAP/PIP/PDP/PEP
- typed obligations와 decision/enforcement record 분리
- UsageLedger와 BudgetLedger
- hierarchical atomic reservation
- bounded BudgetPermit와 fencing
- provider usage provisional/settlement/reconciliation
- completion reserve
- `finish_current_turn`, `hard_stop`, `checkpoint_and_pause`, `degrade_then_finalize`
- Approval과 Review 분리
- Check/Metric/Gate/Trial/ResultBundle
- local SQLite backend와 race/fault TCK

### 핵심 시험

```text
A. quota threshold를 generation 중 초과
B. provider cancel 미지원
C. 늦은 final usage 도착
D. effect commit critical section 진입
E. parallel task가 동시에 마지막 budget을 reserve
```

### 종료 기준

- oversubscription이 허용된 overdraft를 넘지 않는다.
- finish-current-turn은 declared continuation envelope 밖의 새 work를 시작하지 않는다.
- hard-stop 이후 승인된 sequence를 넘는 delta가 client/durable state에 commit되지 않는다.
- telemetry outage가 quota, audit, recovery correctness에 영향을 주지 않는다.

### Current implementation slice

- `UsageLedger` reconciliation now enforces one final reconciliation per source usage record in
  both Python and Rust in-memory/SQLite ledgers, preventing late provider usage from being
  double-counted by multiple reconciled records for the same provisional measurement.
- `ExhaustionController` now models `checkpoint_and_pause` as safe suspension work: checkpoint and
  cleanup can proceed after exhaustion without requiring a top-up continuation permit, while new
  provider work, finalization, optional tasks, and trials remain denied. Explicit continuation
  step/usage bounds still apply, and the behavior is covered by the shared exhaustion TCK.
- `degrade_then_finalize` now admits only best-effort finalization and cleanup after exhaustion
  without requiring a top-up permit. Optional tasks, state-changing effects, unreserved provider
  calls, and other new work remain denied; explicit continuation bounds still apply and are covered
  by the shared exhaustion TCK.

## 7. Phase 4 — Packaging, Integrations, Observability

### Packaging

- Maturin mixed Rust/Python wheel
- foundation release train
- independent first-party extension SemVer
- static entry-point manifest와 lazy loading
- package closure/lock/doctor
- SBOM, license, vulnerability and signature pipeline

### 첫 integrations

```text
model provider: one OpenAI-compatible adapter + scripted provider
parser: PDF + plain text
retriever: local/in-memory + Qdrant adapter
blob: local + S3-compatible
record/state: SQLite/Postgres
telemetry: OTLP
LLM observability: Langfuse adapter
interoperability: Haystack Component/Pipeline adapter
```

### Observability

- canonical observation model
- versioned OTel mapping adapter
- Langfuse telemetry/prompt/evaluation/dataset SPI
- capture/redaction before all exporters
- low-cardinality metric linter
- diagnostic bundle

### 종료 기준

- `pip install graphblocks`에 provider/parser/cloud SDK가 포함되지 않는다.
- plugin discovery만으로 heavy SDK가 import되지 않는다.
- Langfuse/OTLP failure가 run을 실패시키지 않는다.
- AuditLog와 UsageLedger는 lossy exporter를 사용하지 않는다.

### Current implementation slice

- `graphblocks-runtime-core::observability` now models telemetry exporter routes with explicit
  reliability (`durable`, `lossless`, `lossy`) and rejects routing required durable records such as
  `RequiredAudit` and `UsageLedger` to lossy OTLP/Langfuse-style projections while still allowing
  ordinary spans and metrics on lossy exporters.
- Runtime-core observability now records telemetry export outcomes with an explicit
  `run_impact = none` contract. Exporter failures can be retryable and diagnostic, but any exporter
  outcome that claims to fail, pause, bill, quota, or otherwise affect run correctness is rejected.

## 8. Phase 5 — Remote Workers, Release, Deployment (`GB-C4-PRODUCTION`)

### 구현

- versioned worker protocol와 WorkerAdvertisement
- remote edge serialization, ArtifactRef transfer, trace/policy/budget context propagation
- RunOwnershipLease와 fencing
- immutable GraphRelease/DeploymentRevision/PhysicalExecutionPlan
- OCI bundle, image/package/prompt/policy/index lock
- worker drain and in-flight upgrade policy
- Kubernetes/Helm renderer
- Terraform requirements/output bridge
- canary/shadow/rollback quality gates
- SLO and recovery profile

### target images

```text
control-plane
rag-cpu
document-cpu
ocr-gpu
sandbox
```

### 종료 기준

- incompatible package/protocol worker는 admission되지 않는다.
- remote boundary의 non-serializable 또는 oversized inline value를 compile 시 거부한다.
- old release의 conversation/job affinity를 보존하며 drain할 수 있다.
- signed release와 physical plan hash가 모든 run provenance에 남는다.

### Current implementation slice

- `graphblocks-runtime-core::deployment` now includes a typed worker advertisement/admission
  contract. `WorkerAdmissionRequirement` rejects live workers whose advertised target,
  worker-protocol version, package lock hash, or required capabilities do not match the physical
  execution requirement before remote execution is admitted.
- `graphblocks-runtime-core::typed_value` now includes `RemoteBoundaryValuePolicy`, which rejects
  non-serializable inline raw bytes and oversized inline values at remote execution boundaries while
  allowing large payloads to cross by `ArtifactRef`.
- `RemoteExecutionEnvelope` now records the remote target, worker, run/node/attempt/release ids,
  trace context, policy snapshot, optional budget permit, and typed input payload digests as a
  stable handoff contract before work crosses a remote worker boundary.
- Remote boundary value validation now has deterministic compile/deployment diagnostics:
  `GB7001` for non-serializable inline encodings and `GB7002` for oversized inline values that
  should cross the boundary as artifact references.
- Production run provenance now has deterministic diagnostics: `GB7101` for missing signed release
  digest, `GB7102` for missing physical execution plan hash, and `GB7103` for missing release
  signature digest.
- `OciReleaseBundleManifest` now records release bundle layers with path, media type, digest, and
  size, computes a stable bundle manifest digest, and rejects mutable or empty production layer
  references before publishing.
- `KubernetesTargetRenderer` now projects deployment target profiles into deterministic Kubernetes
  `Deployment` manifests and emits `Service` manifests for service-style targets while preserving
  target id, image role, execution host, replica count, and package lock metadata.
- `HelmTargetRenderer` now projects the same deployment target profiles into deterministic
  Helm values, requiring digest-pinned images for every target and exposing a stable values digest
  for release/deployment provenance.
- `TerraformOutputRequirementSet` now records required infrastructure outputs, produces stable
  requirement digests, and validates Terraform output maps for missing or type-mismatched values
  before they are bound into deployment configuration.
- `WorkerDrainPlan` now blocks new work from draining workers by routing new admissions to a
  replacement worker while preserving existing conversation/job affinity on the old worker until
  those affinities complete.

## 9. Phase 6 — Adaptive Orchestration and Verified Work (`GB-X1-ORCHESTRATION`)

### 구현

- bounded TaskPlan/TaskPlanPatch와 revision CAS
- ModelPool/WorkerProfile eligibility
- per-task budget reservation
- context-access graph
- ResourceSnapshot/ChangeSet workspace lifecycle
- isolated Trial, Check/Gate, Review, CAS commit
- LeasePool for scarce resources
- TUI client using Application Protocol

### Acceptance applications

- bounded multi-worker research orchestrator
- authority-backed advisory workflow with official source revalidation and substantive review
- verified workspace optimizer using RTL/Verilog as a fixture
- TUI workspace assistant

### 종료 기준

- model은 GraphSpec topology를 수정하지 않는다.
- TaskPlan limits, dependency acyclicity, context access, budget가 validation된다.
- trusted oracle/test/source는 candidate mutation에서 보호된다.
- review subject digest가 변경되면 review가 자동 무효화된다.

### Current implementation slice

- `graphblocks-runtime-core::orchestration` includes bounded `TaskPlan` and `TaskPlanPatch`
  revision-CAS semantics, dependency/cycle validation, context-resource validation, model/worker
  eligibility, child budget delegation, and `LeasePool` fencing for scarce resources.
- `TaskPlanPatch` validation now rejects duplicate upsert step ids before patch application, so
  model-authored plan edits cannot rely on ambiguous last-write-wins behavior.
- `TaskPlan::context_access_graph` now derives deterministic resource-conflict edges from declared
  task context access, serializing write/read and write/write access separately from
  model-authored task dependencies.
- `WorkspaceHead::commit` now provides a compare-and-swap commit boundary for `ChangeSet`
  candidates, requiring the expected base revision/digest and rejecting denied mutations,
  non-passing gates, or stale/non-accepting reviews before advancing the workspace revision.
- `WorkspaceTrialPlan` now materializes a commit request only after a verified trial has the
  required passing checks, passing gate, active trial-scoped leases, allowed mutation decision, and
  valid review scopes for the candidate digest.
- `graphblocks-runtime-core::tui::TuiRunView` now projects `GetRunStatus` and `AttachToRun`
  replay results into duplicate-tolerant terminal rows, preserving cursor-expired recovery metadata
  without making the TUI the source of truth for run state.

## 10. Phase 7 — Optional Extensions

### Voice (`GB-X2-VOICE`)

DuplexSession, transport, VAD authority, interruption classifier, playback ledger, provider realtime adapter를 별도 package에서 구현한다.

### Current implementation slice

- `graphblocks-runtime-core::voice` now includes the core duplex session contract, transport
  metadata, VAD authority, interruption classifier, playback ledger, realtime session request, and
  a pure `RealtimeProviderAdapter` projection that binds provider id, endpoint, auth secret ref,
  defaults, options, and stable provider-session digests without adding a network client to
  runtime-core.

### Durable unbounded stream (`GB-X3-DURABLE-STREAM`)

offset, partition, watermark, late data, trigger, checkpoint barrier, idempotent sink commit이 필요한 경우에만 구현한다. 문서 ingestion의 finite per-item checkpoint와 혼동하지 않는다.

### Current implementation slice

- `graphblocks-runtime-core::durable_stream` now provides the first durable stream extension
  primitives: source cursors and replay filtering, event-time watermarks with allowed lateness,
  checkpoint barriers covering source cursors, operator state, pending effects, sink commits, plan
  hash and schema versions, delivery guarantee literals, and an idempotent sink commit log that
  accepts exact replays while rejecting mutated idempotency-key reuse. This remains a contract layer,
  not a default stream engine dependency.
- `graphblocks-runtime-durable::InMemoryDurableSource` now tracks committed cursors per stream
  partition. A commit for one partition does not hide uncommitted events in another partition, and
  an explicit replay cursor only overrides replay for its own partition.
- Durable event-time window accumulation now treats watermarks as monotonic: stale watermark
  updates cannot move the late-data boundary backward or make already-late events admissible again.

## 11. CI/CD와 품질 게이트

모든 PR:

```text
format/lint
Rust/Python unit tests
schema generation diff
canonical hash golden tests
TCK subset
package dependency closure
secret/content capture lint
license and vulnerability scan
```

Release candidate:

```text
full TCK
acceptance applications
fault/chaos tests
performance benchmark
wheel matrix
OCI image build
SBOM/provenance/signature
upgrade/downgrade migration tests
```

## 12. 초기 구현에서 의도적으로 하지 않을 것

- 모든 provider와 database 동시 지원
- Kubernetes operator부터 구현
- 범용 distributed stream engine
- arbitrary Python object serialization
- model이 graph topology를 직접 생성/수정
- domain-specific official package
- exactly-once라는 추상적 보장
- token delta별 telemetry span

## 13. 첫 backlog 순서

1. canonical schema repository와 IDs
2. normalized IR/hash
3. BlockDescriptor와 compiler diagnostics
4. Rust scheduler skeleton
5. journal/state/cancel TCK
6. PyO3 binding
7. scripted model + conversation vertical slice
8. document lineage + local parser adapter
9. local Retriever + RAG vertical slice
10. policy/budget/usage race tests
11. package/plugin manifest and lock
12. OTLP/Langfuse projection
13. Python worker protocol
14. immutable release/physical plan
15. Kubernetes renderer
16. TaskPlan/workspace trial
