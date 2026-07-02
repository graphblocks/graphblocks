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
  Exactly-once delivery is not promised.
- Resume from callback re-evaluates policy, budget, release compatibility, ownership lease,
  worker availability, callback authenticity, and idempotency state.
- Large callback payloads are rejected or converted to `ArtifactRef`; callback payloads are always
  untrusted content.

### Current implementation slice

- `graphblocks-runtime-core::run_store::RunStatus` now includes the durable async lifecycle states
  `admitted`, `waiting_input`, `waiting_approval`, `waiting_review`, `waiting_callback`,
  `paused_budget`, `paused_callback_delivery`, `paused_policy`, `paused_operator`, `resuming`, and
  terminal `expired`; SQLite persistence tests cover the new state strings.
- `RunInvocationMode` now records `sync`, `accepted`, and `background` invocation mode in
  `RunRecord`, persists the mode through SQLite, and builds accepted/background run handles with
  event stream, websocket, cancel route, and initial cursor fields.
- Run invocation route diagnostics now report accepted/background routes without cursor-replayable
  event streams as `GB6005`.
- Run invocation route diagnostics now report accepted/background routes tied to
  `client_connection` lifetime as `GB6009`.
- Run invocation route diagnostics now compare declared event retention to reconnect/replay
  guarantees and report insufficient retention as `GB6013`.
- Run status snapshots now expose the protocol response shape with state, release id, last cursor,
  started/updated/completed timestamps, wait reasons, and active async operation ids.
- `RunOwnershipLease` now provides run-scoped coordinator ownership fencing in both in-memory and
  SQLite run stores, including active-lease rejection, stale epoch rejection, and failover after
  expiry.
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
  and a deterministic fuzz-style idempotency sequence.
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
- Callback rejection paths now emit durable `ExternalCallbackRejected` metadata events for stale
  attempts, schema mismatches, and payload-limit failures without journaling rejected payload bodies;
  SQLite persistence covers these rejection events across reopen.
- Async operation configuration diagnostics now report missing callback timeout (`GB6001`), missing
  idempotency key (`GB6003`), and missing callback schema (`GB6007`) in deterministic order for
  top-level `asyncOperations` and `async.start_operation`/`async.await_callback` node configs, with
  shared compiler TCK coverage.
- Async operation configuration diagnostics now compare declared expected callback payload size to
  the configured ingestion limit and report oversized inline callback payloads as `GB6010`.
- Async operation configuration diagnostics now report callback waits that can resume without
  policy, budget, and release-compatibility re-evaluation as `GB6008`.
- Async operation configuration diagnostics now report callback waits without attempt fencing,
  where stale callbacks could resume newer attempts, as `GB6015`.
- Async operation configuration diagnostics now report callback waits that can resume without run
  ownership lease or fencing protection as `GB6016`.
- `SqliteAsyncOperationStore` now persists async operations, operation event journals, and external
  callback receipts across reopen, including idempotency-key duplicate detection after restart.
- Callback receipt duplicate detection is now scoped by `(operation_id, idempotency_key)` in both
  in-memory and SQLite async operation stores, so provider delivery keys reused by separate
  operations do not suppress valid callback receipts or resume signals.
- Callback resume admission can now pause after a durable callback receipt when budget policy
  denies continuation; the operation records `CallbackReceived`, emits a pause reason, and returns
  `should_resume = false`.
- Callback resume admission also records policy-denied and release-incompatible resume decisions
  after durable callback receipt, preserving the journal-before-resume rule while preventing
  scheduler continuation.
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
- Callback event filters now include visibility, node ID, operation ID, and minimum severity
  predicates in addition to event type and terminal-event inclusion.
- `SqliteCallbackDeadLetterStore` now persists callback dead-letter records across reopen and can
  redrive them while preserving original delivery identity, idempotency key, attempt history, and
  audit-visible redrive count.
- Mandatory callback failure policies now map terminal delivery failures to explicit runtime
  actions: pause the run for `pause_run_on_failure`, fail the run for `fail_run_on_failure`, and
  avoid run terminal actions for ordinary retry/dead-letter subscriptions.
- Ordered callback delivery now tracks the blocking delivery per subscription/run and prevents later
  events from scheduling until the prior delivery succeeds, is acknowledged, fails terminally,
  dead-letters, is cancelled, or expires.
- Webhook delivery envelopes now support required GraphBlocks headers, canonical JSON signing,
  `hmac-sha256` verification, replay-window enforcement, and header/body identity checks.
- `SqliteCallbackDeliveryQueue` now persists pending and retry-scheduled callback deliveries across
  reopen, preserving delivery status, idempotency keys, sequence ordering, and retry due times.
- `WebhookDeliveryWorker` now processes due durable callback deliveries with signed webhook
  envelopes, an injected transport boundary, and persisted success/retry outcomes.
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
  webhook endpoint failures to `GB6011` for compiler/deployment reporting.
- Callback subscriptions can now explicitly mark forbidden authoritative uses, and diagnostics
  report callback delivery used as a source of truth for run correctness, billing, quota, audit, or
  effect commit as `GB6004`.
- Callback subscription diagnostics now report mandatory callback delivery without retry,
  dead-letter, or fallback policy as `GB6006`.
- Callback subscription diagnostics now report impossible ordered-delivery requests (`GB6012`) and
  mandatory callback failure policies without dead-letter behavior (`GB6014`).
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
  runtime. Nested callback JSON payloads are deep-frozen at ingress so later caller mutation cannot
  corrupt stored callback receipts or idempotency comparisons.
- `graphblocks-server` now also exposes the framework-neutral `GET /runs/{run_id}`
  `GetRunStatus` route, deriving status, release id, replay cursor, timestamps, wait reasons, and
  active operation projection from the authoritative stored application events.
- Stored server application events are immutable snapshots; `/events`, attach/replay, subscription
  replay, and websocket snapshot responses thaw them back to plain JSON payloads.
- `graphblocks-server` now exposes the framework-neutral `GET /runs` `ListRuns` route using the
  same event-derived run status projection, keeping `POST /runs` reserved for `InvokeGraph`.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/attach`
  `AttachToRun` route, replaying stored events after a supplied cursor and returning explicit
  `CursorExpired` recovery metadata when the requested cursor is no longer retained.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/detach`
  `DetachFromRun` route, recording client detach projections while preserving the authoritative
  event stream and current run status. Stored detach projection records are immutable snapshots.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/subscriptions`
  `SubscribeEvents` route, recording run-scoped event subscription projections and replaying
  retained matching events from the authoritative event stream after an optional cursor. Nested
  event filter and delivery configs are immutable snapshots and are thawed back to plain JSON for
  response payloads.
- `graphblocks-server` now exposes the framework-neutral
  `DELETE /runs/{run_id}/subscriptions/{subscription_id}` `UnsubscribeEvents` route, revoking
  subscription projections without deleting the authoritative event stream.
- `graphblocks-server` now exposes the framework-neutral
  `POST /runs/{run_id}/subscriptions/{subscription_id}/ack` `AckEvent` route, recording event
  acknowledgements by event id or cursor without mutating the authoritative event stream. Stored
  acknowledgement projection records are immutable snapshots.
- `graphblocks-server` now exposes framework-neutral `POST /callbacks/register` and
  `DELETE /callbacks/{subscription_id}` `RegisterCallback`/`RevokeCallback` routes, storing
  callback delivery registration projections and replaying retained run-scoped matching events
  without making callback delivery authoritative. Nested event filter and delivery configs are
  immutable snapshots and are thawed back to plain JSON for response payloads.
- `graphblocks-server` now exposes framework-neutral `POST /runs/{run_id}/pause`,
  `POST /runs/{run_id}/resume`, and `POST /runs/{run_id}/expire`
  `PauseRun`/`ResumeRun`/`ExpireRun` routes, recording run-control projections and reflecting
  the latest control state in `GetRunStatus` while preserving the authoritative event stream.
  Stored run-control projection records are immutable snapshots.
- `graphblocks-server` now exposes framework-neutral
  `POST /callbacks/deliveries/{delivery_id}/redrive` and
  `POST /callbacks/deliveries/{delivery_id}/dead-letter`
  `RedriveCallbackDelivery`/`MoveCallbackToDeadLetter` routes, recording operator and reason
  projections while leaving durable callback queue/dead-letter authority in the runtime layer.
  Stored control projections are immutable snapshots so inspection callers cannot mutate redrive or
  dead-letter history after recording.
- Server-level HTTP/TLS webhook client integration and multi-process coordinator failover execution
  remain follow-on slices.

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
  selection locks, local blob adapters, and ingestion manifests with ACL-gated publish records.
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

## 10. Phase 7 — Optional Extensions

### Voice (`GB-X2-VOICE`)

DuplexSession, transport, VAD authority, interruption classifier, playback ledger, provider realtime adapter를 별도 package에서 구현한다.

### Durable unbounded stream (`GB-X3-DURABLE-STREAM`)

offset, partition, watermark, late data, trigger, checkpoint barrier, idempotent sink commit이 필요한 경우에만 구현한다. 문서 ingestion의 finite per-item checkpoint와 혼동하지 않는다.

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
