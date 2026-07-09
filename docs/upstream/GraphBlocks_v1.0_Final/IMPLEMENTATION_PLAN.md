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
  Python `graphblocks-core`, Rust `graphblocks-schema`, and Rust `graphblocks-types`;
  `graphblocks-schema` now exposes the schema-owned `TypedValue` primitive and canonical JSON
  helper, while `graphblocks-types` is a compatibility re-export that no longer depends on the
  compiler crate for typed-value canonicalization. Python `TypedValue` construction now rejects
  non-JSON, Python-only JSON-like shapes, and non-canonical numeric values before they can enter
  schema envelopes, and stores a canonical JSON copy so caller-owned mutable payloads cannot change
  the envelope after creation.
- Python canonical JSON serialization now rejects mappings with non-string object keys before
  hashing or schema-envelope storage, preventing Python's JSON encoder from silently coercing keys.
- Python `SchemaManifest.from_directory` now parses schema documents with strict JSON semantics,
  rejecting non-standard constants such as `NaN` before schema digests or manifest entries are
  produced.
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
  license/vulnerability gate. The same audit path now strips parenthesized PEP 508 version clauses
  such as `name (>=1.0)` before comparing dependency names to the blocked list.
  Python `build-system.requires` entries are now audited through the same blocked-dependency policy,
  so vulnerable build backends cannot bypass package manifest checks. Top-level Python
  `dependency-groups` are also audited, keeping development/test dependency groups inside the same
  supply-chain policy boundary.
- Package catalog loading now validates catalog shape at the file boundary, rejecting non-mapping
  documents, invalid catalog versions, blank spec versions, non-list package collections, malformed
  entries, and missing distribution names before CLI package inspection or lock generation can
  consume partial catalog state.
- Default metapackage dependency parsing now uses the same normalized distribution-name handling
  for parenthesized PEP 508 constraints, so `graphblocks-core (>=1.0)` records a clean
  `>=1.0` lock constraint instead of producing a bogus missing dependency.
- The `graphblocks` Python metapackage manifest now carries the default foundation dependency
  closure (`graphblocks-core`, runtime, stdlib, documents, RAG, conversation, policy, budget,
  usage, and CLI) instead of remaining an empty placeholder, so package doctor checks can verify
  the installable MVP bundle against the catalog.

## 4. Phase 1 — Local Rust Runtime (`GB-C1-LOCAL-RUNTIME`)

### 구현

- Tokio scheduler와 dependency readiness
- typed receive/send port와 bounded channel
- `Outcome<T>` terminal model
- structured cancellation과 resource scope
- timeout, retry, idempotency boundary
- local semaphore/rate limit/lease
- RunStore와 ExecutionJournal의 in-memory/SQLite reference backend
  - SQLite `ExecutionJournal` replay rejects blank record identity, kind, and metadata fields before
    records can re-enter scheduler recovery.
  - SQLite `ExecutionJournal` payloads are now serialized and replayed with strict JSON semantics,
    rejecting non-standard constants such as `NaN` before corrupted journal payloads can be treated
    as recovery input.
  - `graphblocks run` can persist the local Python runtime's `RunStore` and `ExecutionJournal`
    evidence to caller-selected SQLite stores, so the scripted conversation vertical slice can be
    rerun with durable run status and replayable journal records during MVP acceptance checks.
  - Local Python run stores and `graphblocks run --run-id` now preserve caller-selected run ids
    across generated run records and persisted journals, giving acceptance scripts stable handles
    for replay, status, and evidence correlation.
  - Rust `InMemoryRunStore` and `SqliteRunStore` now expose the same caller-selected run-id
    semantics as the Python facade, rejecting blank/duplicate ids and skipping generated-id
    collisions so durable replay handles stay stable across reference implementations.
  - The Rust stdlib runtime, PyO3 binding, Python runtime wrapper, and `graphblocks run --runtime
    native --run-id` path now accept caller-selected run ids through a canonical runtime options
    payload. The same native options path accepts SQLite `runStorePath` and `journalStorePath`
    values and persists the completed Rust-native run record plus replayable execution journal
    records for MVP evidence checks.
  - `graphblocks-stdlib.run_native_stdlib_graph` forwards the same `run_id`, `run_store_path`, and
    `journal_store_path` options so package users can request stable native evidence handles
    without dropping to the lower-level runtime wrapper.
  - `graphblocks-runtime.run_test_graph` and `graphblocks-testing.run_native_test_graph` accept
    caller-selected `run_id`, `run_store_path`, and `journal_store_path` options for deterministic
    native test/TCK evidence, and the PyO3 bridge preserves the requested id in the top-level
    result, persisted run store, and every execution-journal record.
  - The Python `graphblocks-runtime` wrapper now parses native JSON results with strict JSON
    semantics, rejecting non-standard constants such as `NaN` before PyO3 bridge output can be
    treated as a Python result object.
  - `graphblocks-testing.TckRunner(profile="native")` can execute runtime TCK cases through the
    Rust native test bridge when fixtures provide `nativeNodeOutputs`, using deterministic
    `tck-...` run ids and reporting native journal kinds in observed conformance evidence. Runtime
    cases without `nativeNodeOutputs` fall back to the local reference runtime with explicit
    fallback metadata so shared semantic failure fixtures remain runnable under the native profile.
    The `graphblocks-tck run runtime ... --profile native --json` CLI path reports the same native
    or fallback evidence for shared runtime TCK acceptance checks. The shared runtime TCK fixtures
    annotate their currently successful deterministic cases with `nativeNodeOutputs`, and the TCK
    CLI accepts `--evidence-dir` to persist per-case native SQLite run/journal evidence. `run-all`
    namespaces that evidence by suite id, for example `runtime/tck-...-runs.sqlite3`. TCK reports
    include a `native_evidence` summary when native cases, fallback reasons, or evidence paths are
    present.
  - `graphblocks observe run` can read the Rust `SqliteRunStore` layout emitted by native runtime
    evidence, including Rust's canonical `completed` terminal status.
  - `graphblocks observe journal` can replay persisted SQLite `ExecutionJournal` records for one
    run, exposing terminal kind and ordered records as JSON for CLI-level acceptance evidence.
    The Python observer understands both the Python `sequence` journal schema and the Rust
    `run_sequence`/`record_id` schema emitted by native runtime evidence.
  - Python `Outcome` records now deep-freeze nested metadata mappings and sequences, so terminal
    readiness decisions cannot be changed through retained caller references after publication.
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
- Python `ToolDefinition`, `ToolBinding`, `ToolImplementation`, and `ResolvedTool` facades now
  validate schema refs, tool names, binding ids, execution targets, policy snapshot ids, and
  digests as exact contract identities, rejecting values that only become valid after trimming
  whitespace before canonical hashes or run provenance can depend on them.
- Python `ResolvedTool.valid_until` now validates as a strict RFC 3339-style datetime during tool
  resolution, rejecting space-separated, timezone-less, lowercase-`z`, malformed fractional, and
  compact-offset forms before an expired or malformed capability can reach admission.
- model invocation 전에 application/graph/principal/tenant/conversation/data-classification/deployment/budget intersection으로 `ResolvedTool` set을 생성하고 run provenance에 기록한다.
- Python `ToolCatalog` now validates definition and binding collections, binding item types,
  resolution scope objects, and effective policy snapshot ids before capability intersection can
  produce model-visible `ResolvedTool` records.
- `ToolCallDraft`는 streaming argument fragment만 표현하며 side effect를 실행할 수 없다.
- final `ToolCall`은 schema-valid immutable arguments와 `arguments_digest`를 가진다. argument mutation은 revision과 approval을 invalidation한다.
- Python tool-call drafts and final tool calls now validate response ids, tool-call ids,
  resolved-tool ids, tool names, argument digests, and dependency ids as exact lifecycle
  identities before argument assembly, admission, or dependency planning can depend on them.
- Python `ToolCallDraft` now enforces status/fragment/sequence invariants, so proposed drafts
  cannot carry argument fragments and streaming or complete drafts cannot carry impossible fragment
  counts before JSON assembly creates an immutable `ToolCall`.
- Python `ToolCall` now enforces status/timestamp consistency, so pre-admission calls cannot carry
  admission timestamps, admitted or running calls must carry `admitted_at`, non-terminal calls cannot
  carry `completed_at`, and completed calls must prove prior admission before durable completion.
- tool admission sequence는 resolve, JSON parse, input schema validation, `before_tool_or_effect` policy, budget/resource permit, approval, sandbox/target allocation, idempotency key, effect precondition, execution, result validation/redaction, usage/effect outcome 기록 순서로 고정한다.
- `ToolResult`는 final durable result이고 incremental tool output은 draft projection으로만 취급한다.
- Python `ToolResult` now validates `tool_call_id` as an exact lifecycle identity, rejecting
  whitespace-wrapped ids before output validation, audit records, or model feedback can bind to them.
- Python `ToolResult` artifact references now validate artifact ids, URIs, media types, checksums,
  etags, versions, and filenames as exact metadata values, rejecting strings that only become valid
  after trimming whitespace before untrusted tool output can publish durable artifact references.
- Python `artifact_ref` content parts apply the same exact metadata checks to inline tool-result
  artifact references before those content parts can enter output validation or model delivery.
- `ToolExecutionPlan`은 parallelism, dependency failure policy, cancellation policy, effect serialization key를 명시한다. conflicting state-changing effects는 concurrently 실행하지 않는다.
- Python `ToolExecutionPlan` and `ToolPlanCall` validation now treats plan ids, response ids,
  effect serialization keys, failure/cancellation policy literals, and per-call cancellation
  literals as exact scheduling values, so whitespace-normalized keys cannot alter dependency or
  state-changing effect serialization. Effect-key templates now reject non-string inputs through
  the same typed plan error surface before placeholder expansion begins.
- Rust `ToolExecutionPlan` validation rejects duplicate dependency references per call before
  dependency graph normalization, so dependent tool execution remains deterministic and auditable.
- Rust `ToolExecutionPlan` validation now reports `UnsafeParallelEffects` when independent
  state-changing calls share an effect serialization key, so conflicting writes are rejected before
  scheduling unless an explicit dependency serializes them.
- Remote-service, MCP, and OpenAPI streaming tool-result adapters now force every delta content
  part to carry the adapter-owned `untrusted_external` trust designation, preventing streamed
  tool output or caller-supplied `ContentPart` metadata from self-labeling as trusted model
  context.
- `PolicyRequest.enforcement_point`에 `on_generation_chunk`, `before_client_delivery`, `before_output_commit`, `before_tool_or_effect`를 추가한다.
- The Python policy facade now validates policy request occurrence, decision evaluation/expiry,
  enforcement record occurrence, and policy-test evaluation timestamps as RFC 3339-style datetimes,
  rejecting space-separated forms, timezone-less values, and compact offsets such as `+0000`
  before policy decisions or enforcement evidence can be projected.
- `OutputPolicyDecision`, `OutputDeliveryPolicy`, `OutputCutoff` schema와 terminal semantics를 canonical contract로 추가한다.
- `OutputPolicyDecision` redaction instructions validate replacement values at construction time,
  so malformed redaction payloads cannot enter the delivery gate and fail after partial state
  inspection.
- `OutputPolicyDecision` validation now rejects disposition/content mismatches, such as replacement
  chunks on `allow` decisions or typed redaction instructions on `replace` decisions, before the
  delivery gate can ignore contradictory policy content.
- Declarative output policy evaluation now rejects zero evaluation timestamps before constructing
  an `OutputPolicyDecision`, so generated decisions always carry a valid evaluation time.
- The Python output-policy facade now validates `OutputPolicyDecision.evaluated_at`,
  `OutputCutoff.occurred_at`, and delivery-gate decision occurrence timestamps as RFC
  3339-style datetimes, rejecting space-separated forms, timezone-less values, and compact offsets
  such as `+0000` before mandatory output gate state is projected.
- Python `OutputPolicyDecision` and `OutputCutoff` now validate durable identities, disposition
  literals, pending-tool-call dispositions, provider cancellation choices, redaction paths,
  reason codes, policy refs, terminal reasons, draft dispositions, and durable-result markers as
  exact values, rejecting values that only become valid after trimming whitespace.
- output delivery path는 `GenerationChunk` normalization → `on_generation_chunk` policy evaluation → policy holdback buffer → `before_client_delivery` → `ApplicationEventStream` → client 순서를 따른다.
- `buffer_until_commit`, `bounded_holdback`, `immediate_draft` delivery mode를 지원한다. policy-sensitive streaming의 recommended default는 `bounded_holdback`이다.
- `buffer_until_commit` and `immediate_draft` delivery policies now reject flush boundaries, keeping
  sentence/paragraph or token-driven release configuration scoped to holdback modes that actually
  flush retained content before commit.
- Output delivery policy validation now rejects holdback limits on `buffer_until_commit` and
  `immediate_draft`, and the Rust compiler reports `HoldbackLimitWithoutHoldback` for the same
  shape before deployment. Holdback size/time semantics remain scoped to `bounded_holdback`.
  The Python facade enforces the same constraints even when callers construct
  `OutputDeliveryPolicy` directly instead of using the named factory helpers.
- Python `OutputDeliveryPolicy` now validates delivery modes, violation actions, draft
  dispositions, and flush-boundary names as exact literals, rejecting non-string, empty, or
  whitespace-wrapped values before a streaming output route can be admitted.
- `abort_response`는 local delivery cutoff를 즉시 수행하고 provider/worker cancellation은 cooperative request로 처리한다. local cutoff가 authoritative하다.
- policy-aborted response는 assistant message나 tool result를 durable commit하지 않는다. safe replacement는 새 `response_id`를 사용한다.
- Terminal output-gate decisions validate the constructed canonical `OutputCutoff` before clearing
  pending output or marking the gate stopped, so invalid draft/disposition semantics cannot become
  durable cutoff state.
- `ApplicationEventStream` cutoff enforcement treats event metadata `response_id` as authoritative
  for both normal events and `OutputCutoff` events, and rejects events whose payload `response_id`
  disagrees, preventing malformed late deltas from bypassing an `OutputCutoff` by claiming a
  replacement response in the payload. The Python facade mirrors this state-machine behavior.
- The protocol-level stream state validates canonical `OutputCutoff` terminal reasons,
  draft/durable dispositions, occurrence time, generated/policy/client sequence bounds, and
  keep-draft semantics before recording a response cutoff.
- Protocol replay also validates post-cutoff `AssistantIncomplete` and `AssistantRetracted` events
  against canonical terminal reason, draft disposition, and delivered-sequence metadata before
  allowing them after a response cutoff. Their delivered-sequence metadata must match the accepted
  `OutputCutoff` boundary rather than establishing a new boundary; protocol stream state also
  retains the cutoff policy decision id so draft terminal events cannot mutate cutoff attribution.
- Runtime stream state now persists the accepted cutoff terminal reason with that boundary and
  rejects post-cutoff draft terminal events whose reason differs from the `OutputCutoff`.
- `ApplicationEventStream` runtime state now applies the same canonical post-cutoff draft terminal
  metadata check before accepting `AssistantIncomplete` or `AssistantRetracted` events. The Python
  stream-state facade now rejects post-cutoff draft terminal events whose delivered boundary,
  terminal reason, draft disposition, or policy decision id differs from the stored `OutputCutoff`.
- The PyO3 application-event facade canonicalizes shorthand `AssistantIncomplete` and
  `AssistantRetracted` payloads by adding the implied draft disposition before replaying them
  through the Rust stream state, preserving the runtime invariant while keeping projection fixtures
  stable.
- tool admission validates response-scoped output policy state before applying it; an output-policy
  state object that names a different `response_id` is rejected instead of stopping or authorizing
  another response's tool call. The Python authoring facade mirrors this validation when building
  `before_tool_or_effect` policy requests and when admitting tool calls.
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
- The Python approval facade now rejects space-separated datetimes, compact timezone offsets such
  as `+0000`, and timezone-less values for approval expiration, decision, invalidation, and
  validity-check timestamps before an approval can be treated as current.
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
- Async operation result projection fields now reject bytes-like inputs even though they are Python
  iterables, preventing callback logs or binary payload fragments from being split into integer
  sequences in artifact, diagnostic, metric, check, usage, or external-effect projections.

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
  cover the new state strings. Runtime status snapshots require explicit wait reasons for paused
  callback delivery, so mandatory callback delivery pauses identify the delivery that must be
  redriven, skipped, or dead-lettered.
- `RunInvocationMode` now records `sync`, `accepted`, and `background` invocation mode in
  `RunRecord`; the Python run-store facade validates and persists the mode through SQLite migration,
  and the server builds accepted/background run handles with event stream, websocket, cancel route,
  and initial cursor fields.
- Python run-store creation now reports invalid public invocation modes using the application
  protocol term `invocation mode`, while lower-level record validation still identifies the stored
  `invocation_mode` field.
- Python run-store records now recursively validate inputs, state, and patches as JSON values,
  rejecting arbitrary objects, non-finite numbers, empty or whitespace-wrapped JSON keys, and
  run/graph identities that only become valid after trimming before durable replay or SQLite
  persistence.
- Python SQLite run-store replay now parses stored `inputs_json`, `state_json`, deployment
  provenance, and model-visible tool provenance with strict JSON semantics, rejecting non-standard
  constants such as `NaN` before a corrupted snapshot can be treated as durable run state.
- Rust SQLite run-store replay now rejects malformed model-visible tool provenance fields such as
  non-integer `valid_until_unix_ms`, and the Python SQLite run store rejects malformed
  `model_visible_tools_json` shape, item type, boolean permission, required string identity, and
  `valid_until` fields, so durable run provenance cannot silently drop or coerce a resolved tool
  expiration fence after restart.
- Rust SQLite run-store replay now rejects malformed deployment provenance JSON shape and
  non-string provenance fields, and the Python SQLite run store rejects malformed provenance JSON
  shape during replay, so corrupted release or physical-plan identity cannot be silently replayed
  as missing provenance after restart.
- Run invocation route diagnostics now report accepted/background routes without cursor-replayable
  event streams as `GB6005`, with shared compiler TCK coverage.
- Run invocation route diagnostics now report accepted/background routes tied to
  `client_connection` lifetime as `GB6009`, with shared compiler TCK coverage.
- Run invocation route diagnostics now compare declared event retention to reconnect/replay
  guarantees and report insufficient or zero durable replay durations as `GB6013`, with shared
  compiler TCK coverage.
- Run status snapshots now expose the protocol response shape with state, release id, last cursor,
  started/updated/completed timestamps, wait reasons, and active async operation ids. A
  `waiting_callback` snapshot must include a callback wait reason whose operation is still listed
  as active, preventing misleading status projections that omit the suspended external operation.
  Non-callback wait and pause states also require matching typed wait reasons for input, approval,
  review, budget, policy, or operator intervention. The Rust runtime core provides a canonical
  protocol JSON projection with camelCase fields and typed `waitingOn` entries for server adapters.
  Status projection also rejects duplicate wait-reason identities, so reconnect and resume clients
  do not observe the same blocker as multiple independent conditions.
- Run status snapshots now reject wait reasons on active non-waiting states such as `running` and
  `resuming`, while still allowing active operation ids, so callback/user-facing wait metadata only
  appears when the run is actually waiting or paused.
- `RunOwnershipLease` now provides run-scoped coordinator ownership fencing in both in-memory and
  SQLite run stores, including active-lease rejection, stale epoch rejection, and failover after
  expiry.
- Run state and status mutations now have lease-fenced APIs in both in-memory and SQLite run stores;
  stale coordinators cannot patch run state or advance status after failover, and SQLite validates
  the lease and mutation in one transaction.
- `ApplicationCommandKind` now includes the async run, attach/replay, subscription, callback
  registration, callback ingestion, pause/resume/expire, redrive, and dead-letter command names
  from the amendment. The Python application protocol facade exports the same command tuple and
  accepts these commands in `ApplicationCommand`; the shared application-protocol TCK now asserts
  the amended command set. Python also exports TCK command-kind and event-kind views that are
  checked against the shared `tck/application-protocol/cases.json` fixture so facade drift is caught
  by the same contract file as the Rust harness.
- `ApplicationEvent` now accepts the async/background run lifecycle and callback ingestion events
  needed by the authoritative event stream, including async operation wait/completion events,
  external callback receipt/rejection, late callback receipt, run resume, budget/policy/operator
  pause, callback-delivery pause, and terminal run outcomes.
- The client-facing application protocol event kind set and shared application-protocol TCK now
  include the same async/background lifecycle and callback ingestion event names, with Python and
  Rust protocol facades reporting the shared contract.
- Application protocol event metadata now preserves optional `operation_id` / `operationId` for
  async operation and callback events, and the shared application-protocol TCK includes an
  `ExternalCallbackReceived` envelope case that proves the identity round-trips in Python and Rust.
- Callback subscription `operation_ids` filters now match the protocol event metadata
  `operation_id` in addition to legacy payload spellings, so async callback events do not need to
  duplicate operation identity in their payload to route to subscribers.
- The Python `EventFilter` facade accepts both authoritative `ApplicationEvent` objects and
  client-facing `ApplicationProtocolEvent` objects, matching protocol-event `operation_id` metadata
  for async callback routing with payload spellings as compatibility fallback.
- `graphblocks-callbacks` webhook envelopes now carry optional `operation_id` in the signed payload,
  allowing receivers to observe the async operation identity used for routing without duplicating it
  inside event payload content.
- The application-protocol TCK runner now passes command metadata sequence/timestamp values through
  the protocol model validators instead of coercing them with `int(...)`, so boolean metadata fields
  surface as protocol errors. The shared protocol-log fixture also maps mutated duplicate event ids
  to `duplicate_event_id_conflict` instead of treating the expected rejection as runner failure.
- Event-envelope metadata now follows the same TCK path: sequence and occurrence timestamps are
  validated by `ApplicationProtocolEventMetadata`, so boolean event stream metadata cannot be
  normalized into cursor/replay ordering fields.
- Protocol-log TCK operations now use the same event metadata validation path before append/replay,
  and expected construction errors are recorded as append errors instead of being masked by integer
  coercion.
- Protocol-log replay limits are now passed through to `ApplicationProtocolLog.replay_after`
  without integer coercion, so boolean replay bounds surface as cursor replay contract errors.
- Stream-cutoff TCK operations also validate application-event metadata before cutoff acceptance,
  keeping malformed boolean sequence/timestamp values out of late-output delivery decisions.
- `graphblocks-runtime-core::async_operation` now contains the in-memory `AsyncOperation` and
  callback ingestion state machine for the first TDD slice.
- Implemented behavior covers operation registration, submitted-to-waiting journal entries,
  schema-validated `ExternalCallbackReceived` records, idempotent duplicate callback handling,
  stale-attempt rejection, terminal expiration/cancellation transitions, diagnostic late callback
  records after terminal states, and the required journal-before-resume ordering.
- Callback receipt timestamps now reject conflicting legacy `completed_at` aliases and direct
  `callback_received` records with terminal completion metadata, preserving a single journaled
  receipt time before resume.
- Focused tests include duplicate delivery, invalid callback schema, stale attempt fencing,
  callback-after-timeout/cancellation, concurrent duplicate callback racing, callback/cancel racing,
  whitespace-only operation registration and callback identity rejection at endpoint and store
  boundaries, and a deterministic fuzz-style idempotency sequence.
- The Python server facade rejects callback scope changes and attempt changes for an existing async
  operation before recording another callback, including unscoped callback submissions that still
  carry an operation attempt fence.
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
- Audit outbox records now reject blank record ids, record types, occurrence times, and failure
  reasons before pending or failed delivery projections can enter retry/audit inspection.
  SQLite replay now parses payloads with strict JSON semantics and rechecks the stored
  `payload_digest`, so corrupted audit rows cannot re-enter pending delivery with non-standard
  constants or mutated payload bodies.
- Observability now exposes typed names for the amendment's required async operation, callback
  delivery, and run attach/detach/replay events, and `ObservabilityObservation` validates metric
  labels against the low-cardinality rule including `operation_id`, `event_id`, and `delivery_id`.
- `CallbackEndpointRef` and `CallbackEndpointAuth` now model callback ingress authentication for
  async operations, with bearer-token, `hmac-sha256`, Ed25519 verifier-boundary, mTLS
  client-identity, and OIDC/JWT verifier-boundary helpers that build `AsyncCallbackSubmission` only
  after authentication succeeds.
- OIDC callback authentication now rejects blank `Bearer` tokens before delegating to the verifier
  hook, so an application verifier cannot accidentally authenticate an empty callback credential.
- Rust `CallbackEndpointRef::new_bound` now carries operation, run, node, attempt, release, and
  tenant identity into the runtime endpoint reference, exposes the same canonical resume binding key
  as the Python callback projection, and rejects stale or wrong-scope callback submissions before
  they can reach journal or scheduler admission. Runtime validation also rejects partial endpoint
  bindings so public-field mutations cannot create ambiguous callback resume identities.
- `CallbackEndpointRef` now validates `expires_at` as an ISO-8601 timestamp at creation time, so
  invalid callback endpoint deadlines are rejected before resume admission. The Python callback
  projection also denies resume when a durable callback receipt was recorded after the endpoint
  expiration, even if the projection is evaluated later with a different clock value.
- Callback resume admission now validates the evaluation `now` timestamp before any admission
  decision, including endpoints without `expires_at`, so malformed policy-evaluation clocks cannot
  produce a resumable callback decision.
- `CallbackResumeDecision` now enforces that only `admitted` decisions may set `can_resume`, so
  stale or expired callback receipts cannot be represented as scheduler-resumable decisions.
- `graphblocks-callbacks` timestamp validation now rejects space-separated datetimes and compact
  timezone offsets such as `+0000` on webhook envelopes, signing headers, endpoint expirations,
  retry projections, dead-letter/redrive timestamps, and external callback receipts, keeping the
  optional callback projection package aligned with the shared RFC 3339-style durable TCK parser.
- Python core `ExternalCallbackReceived` receipt timestamps now reject surrounding whitespace and
  lowercase `z` suffixes before payload digest verification or durable receipt projection, so
  callback journal evidence cannot be silently canonicalized from malformed ingress metadata.
- The Python core callback facade now applies the same timestamp parsing rule to
  `CallbackSubscription` and `CallbackDelivery` timestamps, so subscription lifetimes, retry
  schedules, delivery acknowledgements, and terminal timestamps reject compact offsets and
  space-separated datetime forms before durable projection. The facade now also rejects callback
  timestamps that only become valid after trimming surrounding whitespace, so subscription and
  delivery projections cannot silently canonicalize malformed client input.
- `CallbackEndpointRef` now rejects endpoint URLs with surrounding whitespace before scheme
  validation, preserving exact ingress route identity for signed callback submissions.
- Callback rejection paths now emit durable `ExternalCallbackRejected` metadata events for stale
  attempts, unknown operations, run/node identity mismatches, schema mismatches, payload-limit
  failures, callbacks that arrive after an operation's callback deadline, and callbacks that target
  operations not currently waiting for a callback, without journaling rejected payload bodies;
  SQLite persistence covers these rejection events across reopen.
- Quarantined callbacks that lose admission because an earlier quarantined callback already resumed
  the operation now produce `ExternalCallbackRejected` metadata with
  `quarantined_callback_superseded`, so early-callback race handling remains auditable instead of
  silently dropping distinct queued deliveries.
- Quarantine replay now continues after an audited rejected callback, allowing a later valid
  quarantined callback for the same operation to resume the run while preserving the first rejection
  metadata in the operation event stream.
- Quarantined callback replay uses callback receipt time, not idempotency-key ordering, so the
  earliest valid delivery wins admission even when durable quarantine storage uses keyed maps.
- Async operation configuration diagnostics now report missing callback timeout (`GB6001`), missing
  idempotency key (`GB6003`), and missing callback schema (`GB6007`) in deterministic order for
  top-level `asyncOperations` and `async.start_operation`/`async.await_callback` node configs, with
  shared compiler TCK coverage. `async.await_callback@1` node configs require an expected callback
  schema even when the author omits the optional `callback` mapping, because the block is itself a
  callback wait boundary.
- Async operation configuration diagnostics now compare declared expected callback payload size to
  the configured ingestion limit and report oversized inline callback payloads as `GB6010`, with
  shared compiler TCK coverage. Non-positive `expectedPayloadBytes` and `maxPayloadBytes`
  declarations now produce `InvalidAsyncOperation` instead of silently falling back to the default
  callback payload limit.
- Async operation configuration diagnostics now report callback waits that can resume without
  policy, budget, and release-compatibility re-evaluation as `GB6008`, with shared compiler TCK
  coverage.
- Async operation configuration diagnostics now report callback waits without attempt fencing,
  where stale callbacks could resume newer attempts, as `GB6015`, with shared compiler TCK coverage.
- Async operation configuration diagnostics now report callback waits that can resume without run
  ownership lease or fencing protection as `GB6016`, with shared compiler TCK coverage.
- The normative `graphblocks-compiler` Rust crate now emits the same `GB6001` through `GB6016`
  async/callback diagnostics as the Python authoring facade and passes the shared compiler TCK for
  these cases. `async.poll_operation@1` node configs participate in the same `GB6001` timeout
  diagnostics as callback-backed async waits, and timeout fields must parse to positive durations
  rather than merely being non-empty strings. The compilers also reject graph-authored waits that
  define both a bounded timeout and an explicit infinite-wait policy as `InvalidAsyncOperation`.
  `expiresAtUnixMs` now satisfies the bounded-wait contract as an absolute deadline, but compiler
  diagnostics reject configs that also define relative timeout fields so absolute and derived
  deadlines cannot silently diverge.
  Python compiler diagnostics now also reject non-canonical `resumeTokenHash` values on async
  operation configs as `InvalidAsyncOperation`, so malformed callback fencing digests are caught
  before runtime admission.
  Async wait `onTimeout` actions are validated at compile time as `fail`, `cancel`, or `expire`,
  and poll `interval`/`maxInterval` duration fields are rejected before runtime when they are zero
  or unparsable. The Python authoring facade mirrors these diagnostics so shared compiler TCK
  results stay aligned with the Rust normative compiler.
- `SqliteAsyncOperationStore` now persists async operations, operation event journals, and external
  callback receipts across reopen, including idempotency-key duplicate detection after restart.
- SQLite async operation replay validates that each stored operation JSON identity matches its
  durable row key before the operation can re-enter scheduling, callback admission, or duplicate
  detection state.
- SQLite async operation replay also runs the normal `AsyncOperation` validation invariants over
  stored operation JSON before the operation can be rehydrated.
- SQLite async operation event replay validates that each stored event payload belongs to the
  durable operation row key before it can re-enter the replayable operation journal.
- SQLite async operation event replay also requires event indexes to be contiguous from zero for
  each operation, preserving deterministic journal order across recovery.
- SQLite async operation event replay now revalidates event metadata such as callback ids,
  decision ids, reasons, verifier identity, and occurrence timestamps before rehydrating the
  replayable operation journal.
- Callback receipt duplicate detection is now scoped by `(operation_id, idempotency_key)` in both
  in-memory and SQLite async operation stores, so provider delivery keys reused by separate
  operations do not suppress valid callback receipts or resume signals.
- Callback receipt duplicate detection now rejects idempotency-key conflicts when a replay mutates
  callback identity or payload digest; in-memory, SQLite-reopen, and deterministic fuzz tests verify
  that the original receipt remains authoritative and no second resume is produced.
- Server callback ingress now treats the first accepted receipt for an operation/run/node/attempt
  as the authoritative resume signal and appends an `ExternalCallbackReceived` metadata-only event
  to the run's authoritative `ApplicationEventStream`. Exact idempotent replays return
  `duplicate`; later callbacks for the same bound operation attempt but with a new idempotency key
  are rejected as `duplicate_operation_receipt` and are not appended as accepted submissions.
  Conflicting scoped callbacks for a different run, attempt, or node now also append
  `ServerAsyncCallbackRejection` metadata before returning `409`, preserving audit evidence for
  stale or misrouted callback attempts. Run-scoped callback rejections with known run identity are
  also appended to the authoritative `ApplicationEventStream` as `ExternalCallbackRejected`
  metadata-only diagnostics.
- Server callback ingress now also projects terminal-run callback rejections as
  `LateExternalCallbackReceived` diagnostics, so late arrivals after cancellation, expiry, failure,
  or policy stop remain inspectable without creating an accepted callback receipt or resume signal.
  These diagnostics are appended to the run's authoritative `ApplicationEventStream` after the
  terminal event while preserving the run's terminal status.
- Server callback registration replay now evaluates `visibility`, `nodeIds`, and `operationIds`
  filters against canonical application-event metadata as well as legacy payload fields, so
  metadata-only async callback receipt/rejection events route to operator subscriptions without
  duplicating routing identity in the payload.
- Raw run event replay now enforces the same principal visibility boundary for SSE, attach, and
  WebSocket snapshot responses. Hidden operator/internal/audit events remain part of the
  authoritative cursor sequence, allowing clients to advance cursors without receiving events they
  are not authorized to observe.
- Async callback ingestion now supports durable pre-operation quarantine for the race where an
  external provider replies before the committed `AsyncOperation` is visible. Quarantined callbacks
  are keyed by `(operation_id, idempotency_key)`, persist across SQLite reopen, deduplicate
  repeated provider delivery attempts, and are consumed through the normal journal-before-resume
  callback admission path after operation registration.
- SQLite replay of pre-operation quarantine validates that each stored callback submission identity
  matches the durable `(operation_id, idempotency_key)` row key before it can be promoted into
  normal callback admission.
- SQLite replay of pre-operation quarantine also applies the normal callback submission identity
  and timestamp validation before a stored submission can re-enter callback admission.
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
- Late callbacks against terminal async operations (`completed`, `failed`, `cancelled`, or
  `expired`) are recorded as `LateExternalCallbackReceived` diagnostics and never produce a resume
  signal or state rewrite.
- Python `AsyncOperationResult.from_late_callback(...)` now projects late callback payloads for
  terminal operations as `incomplete` diagnostic results while preserving committed external effect
  metadata, so facade consumers cannot accidentally treat a late callback as a resumable operation.
- Python `graphblocks-core` now exposes an immutable `AsyncOperation` schema facade with the
  amendment states (`created`, `submitted`, `waiting_callback`, `callback_received`, `polling`,
  `resuming`, and terminal states), callback/polling refs, expected schema, resume token hash,
  idempotency key, timestamps, transition helpers, and JSON projection coverage.
- Python `graphblocks-core` now also exposes an immutable `ExternalCallbackReceived` schema facade
  with callback/run/node/attempt identity, provider operation identity, idempotency key, canonical
  payload digest, verified principal, policy snapshot, artifact references, and JSON projection
  coverage. Callback receipts recompute the canonical payload hash and reject mismatched
  `payload_digest` values before a receipt can represent durable journal input. Callback artifact
  references must be JSON objects with non-empty `artifact_id` and `uri` fields, optional
  `media_type` and `checksum` values must also be non-empty when present, and duplicate
  `artifact_id` values are rejected at receipt construction. These artifact string fields are
  exact metadata values and reject surrounding whitespace instead of silently normalizing artifact
  identities or URIs. Optional `size_bytes` values must be non-negative integers. Artifact refs
  accept `artifactId`, `mediaType`, and `sizeBytes` aliases at ingress and project canonical
  snake_case fields. Receipt payloads and artifact objects are stored as immutable JSON snapshots
  and thawed into fresh plain JSON values for `to_json()` callers.
- Python `AsyncOperation` records now validate `resume_token_hash` as a canonical `sha256:`
  digest, so callback resume fencing cannot be represented by an arbitrary label.
- Python async operation, callback receipt, result, and external-effect facades now validate
  operation ids, run/node/attempt ids, callback ids, schema refs, provider operation ids,
  idempotency keys, verifier/policy identities, digest strings, and lifecycle/status literals as
  exact values, rejecting inputs that only become valid after trimming whitespace before callback
  fencing, duplicate detection, or late-effect reconciliation can depend on them.
- Rust `AsyncOperation` validation now applies the same canonical `sha256:<64 lowercase hex>`
  requirement to `resume_token_hash`, keeping durable callback fencing tokens aligned across the
  Python schema facade and Rust runtime backend.
- Shared durable async callback TCK fixtures now use canonical resume-token digest values, and the
  Python testing package asserts the fixture cannot drift back to placeholder callback fencing
  labels.
- Shared durable async callback resume TCK fixtures now validate supplied operation envelopes,
  requiring object shape, nonblank `operationId`, `idempotencyKey`, `runId`, `nodeId`,
  `attemptId`, `releaseId`, `policySnapshotId`, canonical `resumeTokenHash`, nonblank
  `expectedSchema`, ISO `deadline`, nonblank `budgetState` evidence, and `waiting_callback` state
  when operation state evidence is supplied, so journal-before-resume conformance cannot be proven
  from a malformed, anonymous, unpinned, unfenced, terminal, or unbounded `AsyncOperation`
  reference.
- Shared durable async callback resume TCK fixtures now validate supplied callback receipt
  envelopes, requiring nonblank `callbackId`, canonical `payloadDigest`, and nonblank
  `verifiedBy` evidence before journal-before-resume conformance can be proven from callback
  receipt metadata.
- Shared durable async callback resume TCK fixtures now reject callback receipt envelopes whose
  payload schema validation evidence is false, so schema-invalid callback payloads cannot satisfy
  resume conformance.
- Shared durable async callback resume TCK fixtures now reject callback receipts whose
  `verifiedBy` evidence is explicitly `unauthenticated`, so a nonblank placeholder cannot satisfy
  callback authentication conformance.
- Shared durable async callback resume TCK fixtures now reject callback receipt envelopes whose
  signature verification evidence is false, so a failed HMAC, Ed25519, mTLS, OIDC, or
  provider-specific signature cannot satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now reject callback receipt envelopes that
  identify a non-`ExternalCallbackReceived` event type, so ordinary application events cannot be
  used as durable callback receipts.
- Shared durable async callback resume TCK fixtures now compare supplied callback receipt
  `operationId`, `runId`, `nodeId`, `attemptId`, and `policySnapshotId` values against the
  waiting `AsyncOperation` envelope, so stale, misrouted, or policy-snapshot-mismatched callbacks
  cannot satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now require supplied callback receipt envelopes
  to carry a nonblank `idempotencyKey` and ISO `receivedAt` value, so journal-before-resume
  conformance cannot be proven from an undeduplicable or untimestamped callback receipt.
- Shared durable async callback resume TCK fixtures now reject callback receipts whose
  `receivedAt` timestamp is after the waiting `AsyncOperation` deadline, so timed-out callbacks
  cannot satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now require supplied callback receipt envelopes
  to carry `releaseId` evidence that matches the waiting `AsyncOperation`, so a stale receipt from
  an incompatible release cannot satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now require `tenantId` evidence on both the
  waiting `AsyncOperation` envelope and supplied callback receipt, with receipt values matched
  against the operation, so cross-tenant callbacks cannot satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now validate optional `providerOperationId`
  fences: when the waiting `AsyncOperation` pins provider-operation evidence, supplied callback
  receipts must carry the same value before they can satisfy resume conformance.
- Shared durable async callback resume TCK fixtures now require callback receipts to carry
  nonblank `operationId`, `runId`, `nodeId`, `attemptId`, and `policySnapshotId` evidence before
  journal-before-resume conformance can be satisfied.
- The Python `AsyncOperation` facade now enforces the amendment state machine: callbacks must move
  through `waiting_callback` before `callback_received`, polling must be explicit before terminal
  poll results, terminal operations cannot transition again, and direct construction rejects
  callback/polling wait states that omit their required callback or polling reference.
- The Python `AsyncOperation` facade now validates state/timestamp consistency: non-created states
  require `submitted_at`, terminal states require `completed_at`, and `created` records cannot
  already carry submitted/completed timestamps or wait boundaries. Deadline and explicit
  infinite-wait policy fields are introduced only once an operation advances into a submitted wait
  state.
- The Python `AsyncOperation` facade now validates ISO datetime syntax and ordering for
  `created_at`, `submitted_at`, `completed_at`, and `expires_at`, including offset-aware comparisons
  for submitted-before-created, completed-before-submitted, non-positive expiry windows, and
  expiry deadlines that are already elapsed by submission time. `callback_received` records require
  a durable receipt timestamp, and that timestamp is rejected once it falls after the operation
  expiry boundary, so late callbacks cannot be projected as resumable operation state. Polling
  and callback-backed terminal completions or failures also reject `completed_at` values after
  `expires_at`, and `expired` records reject `completed_at` values before `expires_at`, preventing
  late provider results or early timeout projections from being recorded as current operation
  outcomes. The same parser now rejects compact timezone offsets such as `+0000` and
  space-separated datetime forms across async operation lifecycle fields before ordering checks.
- The Python `AsyncOperation` lifecycle validator now also rejects lifecycle timestamps that only
  become valid after trimming whitespace or accepting lowercase `z`, keeping helper-driven
  submissions and callback receipt transitions on the same strict RFC 3339-style boundary as
  direct operation rehydration.
- Rust `AsyncOperation` validation now rejects zero `created_at_unix_ms` values so durable
  operation records cannot start from a sentinel timestamp.
- Rust `AsyncOperation` validation now rejects `created` records that already carry submitted,
  completed, or expiration timestamps, and the Python facade now applies the same lifecycle
  boundary before external submission or waiting.
- Rust `AsyncOperation` validation now rejects whitespace-only `provider_operation_id` values before
  registration, matching callback submission identity checks and preventing unusable provider
  identities from entering the durable operation store.
- Rust `AsyncOperation` records now carry optional `infinite_wait_policy` values through validation
  and JSON store projection; `GB6001` timeout diagnostics are suppressed only when a wait has either
  `expires_at_unix_ms` or an explicit infinite-wait policy, and callback receipt records can use the
  same explicit infinite-wait boundary without requiring an expiration timestamp. Runtime validation
  rejects waits that define both fields, so deadline-bound waits and explicitly infinite waits cannot
  be confused.
- Callback-backed terminal transitions now reject `completed_at` values that precede the durable
  callback receipt timestamp, with deterministic transition fuzz coverage across completed, failed,
  cancelled, and expired outcomes.
- The Python `AsyncOperation` facade now rejects provider operation identity before submission, so
  `provider_operation_id` cannot appear on a still-created operation record and provider invocation
  remains separated from durable operation creation.
- The Python `AsyncOperation` facade now enforces the amendment's bounded-wait invariant at runtime:
  callback and polling waits require either `expires_at` or an explicit `infinite_wait_policy`, but
  not both, with deterministic fuzz coverage for deadline/policy combinations. Directly constructed
  wait states enforce the same boundary so rehydrated operation records cannot bypass the helper
  path.
- `AsyncOperationResult` and `ExternalEffectRecord` now preserve committed external side effects
  even when an async operation result is `cancelled`, `expired`, or `incomplete`; stdlib async
  terminal blocks can project `externalEffects` config into the final result instead of dropping
  provider effect identity. Result projections reject duplicate artifact ids, `effect_id`, and
  `provider_effect_id` values so artifact, audit, and ledger consumers can treat each recorded
  local and provider identity as single-assignment within the operation result. The Rust runtime
  core projects results to canonical protocol JSON with camelCase `externalEffects` entries for
  downstream graph nodes and server adapters.
- Python `graphblocks-core` now exposes the same authoring/schema facade for
  `AsyncOperationResult`, `AsyncOperationResultStatus`, and `ExternalEffectRecord`, including
  validation that provider effect identity is only attached to committed external effects.
- Python `AsyncOperationResult` now validates output, artifacts, diagnostics, metrics, checks, and
  usage projections as strict JSON-compatible values, deep-freezes arrays and object mappings on
  construction, and returns thawed copies from `to_json()` so untrusted callback/result payloads
  cannot be mutated after journaling. Nested JSON object keys must be non-empty exact strings, so
  callback payloads and result projections cannot hide ambiguous keys that only become meaningful
  after trimming. Caller-supplied tuples are rejected rather than accepted as JSON arrays, while
  real JSON lists still freeze internally for immutability. Projection helpers reject malformed
  non-sequence and string inputs, require each projection entry to be a JSON object, and reject
  malformed external-effect sequences before validating individual projection items or effect
  records.
- Python `AsyncOperationResult.from_operation` now projects durable results only from terminal
  `AsyncOperation` records, mapping terminal state to result status while preserving the operation
  id and rejecting non-terminal waits or resumes.
- `graphblocks-runtime-core::stdlib_runtime` now exposes deterministic `async.start_operation@1`
  and `async.await_callback@1` blocks so graph-level examples can start an external operation and
  checkpoint while waiting for callback without treating callback delivery as the source of truth.
  `async.start_operation@1` accepts either an absolute `expiresAtUnixMs` or a relative positive
  timeout duration such as `30m`, deriving the durable callback deadline from `createdAtUnixMs`.
  Rust and Python both validate those millisecond timestamps as unsigned 64-bit values and reject a
  derived expiration that would exceed the timestamp range. Provider submissions whose
  `submittedAtUnixMs` precedes `createdAtUnixMs` are rejected before returning a graph-visible
  operation projection, callback expiration must remain after the submission timestamp, and no
  `waiting_callback` projection is produced without submitted provider metadata. Python runtime
  stdlib async blocks now also reject non-canonical `resumeTokenHash` values in both start-operation
  configs and projected operation inputs. It also accepts an explicit `infiniteWaitPolicy`,
  producing a `waiting_callback` operation with no expiration only when that unbounded wait policy
  is declared.
  Direct stdlib invocation in both Rust and Python now rejects configurations that define both a
  deadline/timeout and `infiniteWaitPolicy`, preserving the bounded-wait invariant even outside the
  compiler path.
  `async.await_callback@1` carries parsed timeout duration config or an explicit
  `infiniteWaitPolicy` into the wait projection so the scheduler can enforce the same boundary it
  compiled, and validates `onTimeout` as one of `fail`, `cancel`, or `expire` rather than accepting
  arbitrary continuation policies. Await projections now also require `checkpoint` to be a boolean,
  avoiding accidental truthy string coercion for durable suspension behavior. Direct stdlib
  operation consumers now validate required operation identity, state, idempotency, resume-token,
  and expected-schema fields before accepting an externally supplied operation projection. The
  Python stdlib boundary normalizes spec camelCase operation identity, provider, wait-policy, and
  timestamp fields to the same canonical snake_case projection that Rust already accepts, keeping
  direct local tests and protocol-shaped operation payloads interoperable. Rust terminal async
  blocks enforce submitted/expiration timestamp bounds against both canonical snake_case and
  protocol camelCase operation projections.
- The stdlib runtime also exposes `async.poll_operation@1`, `async.complete_operation@1`,
  `async.cancel_operation@1`, and `async.expire_operation@1` projections for polling and terminal
  async operation results. `async.poll_operation@1` requires a timeout in its block config so
  graph-authored polling waits cannot silently become unbounded, and accepts positive duration
  strings such as `30s`, `5m`, or `2h` for interval and timeout settings. Parsed string durations
  must fit in unsigned 64-bit milliseconds, matching the integer duration contract. Runtime poll
  projections now also honor an explicit `infiniteWaitPolicy`, omitting `timeoutMs` only when that
  unbounded wait policy is declared, and reject poll configurations whose `maxInterval` is lower
  than the initial `interval`. Terminal stdlib projections now preserve `completedAtUnixMs` for
  successful completions and reject zero `completedAtUnixMs`, `cancelledAtUnixMs`, or
  `expiredAtUnixMs` values, non-object terminal block configs, malformed non-integer terminal
  timestamp fields, and timestamps that regress before `submitted_at_unix_ms` or exceed
  `expires_at_unix_ms`, matching the durable operation-store timestamp invariant. Terminal result
  projections also preserve artifact references and structured `diagnostics`, `metrics`, `checks`,
  and `usage` lists, rejecting malformed non-object entries before returning graph-visible
  `AsyncOperationResult` values. Rust stdlib boundary coverage now also verifies that malformed
  terminal `artifacts` and `externalEffects` with provider identities but no committed outcome fail
  before a result projection is returned.
- Python `graphblocks-core`'s in-process stdlib registry now mirrors those async operation block
  projections for authoring and local tests, including callback wait projection, poll projection,
  terminal result projection, duration-string parsing, and the same terminal timestamp guard for
  cancel/expire results. Python terminal result projections now also preserve configured external
  effect records so cancellation and expiry retain committed side-effect metadata for audit and
  compensation, and reject provider effect identities unless the external effect outcome is
  `committed`.
- Python `graphblocks-core` now exposes immutable callback schema facades for `EventFilter`,
  `CallbackSubscription`, and `CallbackDelivery`, covering subscription scope/status/failure-policy
  validation, terminal-event filter projection, delivery idempotency metadata, and terminal delivery
  timestamp invariants.
- Python callback schema facades now reject whitespace-wrapped filter selectors, subscription
  identities, replay cursors, lifecycle/failure-policy literals, delivery identities,
  idempotency keys, statuses, and terminal error text before callback projections can be stored or
  redriven.
- `graphblocks-callbacks` now applies the same exact-value rule to durable webhook delivery,
  envelope, replay, endpoint, authentication-ref, and external callback receipt identities,
  rejecting surrounding whitespace before those values can become replay keys, resume fences,
  idempotency keys, or verifier/principal references.
- `graphblocks-runtime-core::callback_delivery` now contains callback subscription filtering,
  deterministic delivery records, idempotency keys, success/duplicate acknowledgement handling,
  bounded retry scheduling, best-effort failure handling, dead-letter terminal state, and redrive
  records that preserve original delivery identity, event identity, attempt history, operator, and
  reason. Receiver-provided `Retry-After` delays are capped by the configured retry policy maximum
  so rate-limit responses cannot create unbounded callback retry stalls.
- Callback delivery targets are now typed as webhook, WebSocket, SSE, push notification, email, or
  local callback variants, and ordered-delivery diagnostics use target capabilities instead of
  string-prefix inference. The Rust parser now rejects unknown delivery target prefixes instead of
  silently downgrading them to local callbacks, so typoed or unsupported target schemes cannot
  bypass intended callback delivery routing.
- `graphblocks-server` now enforces callback delivery target safety at registration and subscription
  admission: webhook delivery requires HMAC-SHA256 or Ed25519 signing metadata and rejects obvious
  forbidden egress targets such as localhost, private/link-local IPs, `file://`, Unix socket URLs,
  and URLs with embedded userinfo credentials.
- Runtime webhook target validation now also rejects alternate numeric IPv4 literals that resolve to
  forbidden internal addresses, such as decimal or hex loopback forms, before delivery can reach an
  HTTP transport.
- Callback event filters now include visibility, node ID, operation ID, and minimum severity
  predicates in addition to event type and terminal-event inclusion. Runtime filters now treat
  `include_terminal_events: false` as an explicit terminal-event exclusion even for otherwise broad
  subscriptions, and the framework-neutral server replay facade applies the same rule for
  `includeTerminalEvents`.
- Callback subscription validation now rejects blank node and operation selector values, plus
  malformed severity selector mutations, before the filter can create an ambiguous event projection.
- `SqliteCallbackDeadLetterStore` now persists callback dead-letter records across reopen and can
  redrive them while preserving original delivery identity, idempotency key, attempt history, and
  audit-visible redrive count. Repeated redrives append each redriven attempt to the durable
  attempt history so operator redrive audits cannot reuse an attempt number after restart.
- Callback dead-letter records can now project an operator redrive back into a pending delivery
  without minting a new application event identity, preserving the original delivery, event,
  subscription, run, cursor, and idempotency identifiers while advancing the delivery attempt.
- Callback dead-letter records now reject inconsistent projections whose wrapped delivery is not
  `dead_lettered` or whose attempt history omits the dead-lettered delivery attempt. Dead-letter
  projection preserves the actual delivery attempt history even if the retry policy was reduced
  before the delivery moved to dead letter.
- Mandatory callback failure policies now map terminal delivery failures to explicit runtime
  actions: pause the run for `pause_run_on_failure`, fail the run for `fail_run_on_failure`, and
  avoid run terminal actions for ordinary retry/dead-letter subscriptions. `graphblocks-callbacks`
  exposes this as a typed delivery-failure action decision so runtimes can apply the policy without
  reinterpreting raw delivery status strings.
- Callback delivery persistence now rejects success-state records that have lost their required
  timestamps: `delivered` requires `delivered_at_unix_ms`, and `acknowledged` requires
  `acknowledged_at_unix_ms`. Queue validation covers malformed replay records before they can
  re-enter the worker or ordered-delivery state machines.
- Callback delivery persistence also rejects terminal failure records without a nonblank
  `last_error`, so failed, dead-lettered, cancelled, or expired delivery records keep the reason
  needed for mandatory pause/fail actions, operator redrive, and audit review. The Python core
  callback facade now enforces the same `last_error` requirement before a terminal failure
  projection can be represented.
- Runtime record-store operations now reject whitespace-only collection names and record keys
  before writes, reads, queries, or deletes. This keeps durable record identities usable as stable
  row keys, cursors, compare-and-swap targets, and audit references rather than admitting visually
  blank state entries.
- Local and S3-compatible blob stores now reject whitespace-only blob keys and key segments before
  artifact writes, reads, listings, or metadata projection. This prevents visually blank artifact
  identities from entering callback payload handling, artifact references, and replayable storage.
- Blob-store put options, user metadata keys/values, metadata etags, list cursors, and
  S3-compatible bucket/scheme identities now reject values that only become valid after trimming,
  so artifact references are not normalized differently by local and remote storage adapters.
- Local blob-store metadata replay now parses sidecar JSON with strict JSON semantics and requires
  an object root, rejecting non-standard constants such as `NaN` before persisted artifact metadata
  can be projected through `head` or `list`.
- Application protocol replay now treats whitespace-only or whitespace-wrapped cursors as invalid
  replay positions and returns no unretained replay instead of widening the request to the beginning
  of the authoritative event stream.
- Durable sink commit records now require structured JSON object metadata, so idempotent external
  effect commits cannot be represented by opaque scalar payloads that lose audit, reconciliation,
  or compensation fields.
- Callback ingress deployment validation now requires every `SubmitAsyncCallback` route to include
  an `{operation_id}` path binding, keeping external callback endpoints tied to a concrete async
  operation identity before authentication, idempotency, and resume fencing are evaluated.
- Async callback endpoint references now reject non-HTTP(S) URLs at construction/validation time,
  preventing `file://`, socket-like, or other non-ingress schemes from becoming authenticated
  callback resume endpoints.
- Async operation result validation now requires `diagnostics`, `metrics`, `checks`, and `usage`
  entries to be structured JSON objects before downstream graph nodes consume callback or polling
  results.
- Observability metric label validation now rejects whitespace-only label keys or values before
  applying the high-cardinality denylist, keeping async/callback telemetry dimensions usable for
  bounded aggregation.
- Diagnostic bundle validation now rejects blank bundle, run, and excerpt identities before
  redaction checks, keeping exported support/audit bundles addressable and replay-safe.
- Callback delivery IDs and receiver idempotency keys now percent-encode subscription and event
  identity components before joining them. This preserves the existing readable form for simple
  IDs while preventing `_`, `:`, `%`, or non-ASCII component collisions during replay and redrive.
- Callback delivery queue validation now rejects blank persisted delivery identity fields
  (`delivery_id`, `subscription_id`, `event_id`, `run_id`, `cursor`, and `idempotency_key`) so
  malformed durable records cannot re-enter replay, worker recovery, signing, or redrive paths.
- Callback dead-letter storage now validates the same durable identity fields plus non-empty
  attempt history before insert and after load, preventing malformed dead letters from producing
  ambiguous redrive attempts after restart.
- Callback dead-letter construction now validates the source delivery identity before projecting
  audit/redrive metadata, so malformed in-memory deliveries cannot bypass the durable store checks.
- SQLite callback dead-letter replay validates that the stored dead-letter identity matches the
  durable row key before a record can be returned or redriven.
- Dead-letter attempt history must now be consecutive from attempt `1`, so redrive after restart
  cannot reuse attempt `0`, skip an attempt, or create duplicate attempt numbers in audit history.
- Dead-letter persistence now also requires a nonblank `last_error`, keeping every dead-lettered
  delivery tied to the terminal failure reason that operators review and redrive.
- SQLite callback dead-letter updates now reject immutable original delivery metadata conflicts and
  redrive-history regressions, so a later write cannot overwrite the delivery identity or erase
  preserved redrive attempt history.
- Ordered callback delivery now tracks the blocking delivery per subscription/run and prevents later
  events from scheduling until the prior delivery succeeds, is acknowledged, fails terminally,
  dead-letters, is cancelled, or expires. Replay scheduling uses the same ordered gate so retained
  events for an ordered subscription enqueue only the first currently unblocked delivery for a run.
- Webhook delivery envelopes now support required GraphBlocks headers, canonical JSON signing,
  `hmac-sha256` verification, replay-window enforcement, and header/body identity checks.
- Webhook response decisions now validate `retry_after` timestamps at construction time, so direct
  decisions and classified receiver responses share the same retry scheduling contract.
- Callback retry scheduling now normalizes explicit zero `retry_after` delays to a positive delay
  before applying the configured cap, preventing rate-limit responses from creating immediate loops.
- Callback envelopes now validate `occurred_at` and `delivered_at` as ISO-8601 timestamps and
  reject deliveries whose delivery timestamp precedes the source event timestamp.
- External callback receipts now validate `received_at` as an ISO-8601 timestamp and reject receipt
  records whose durable receipt time precedes the callback envelope delivery time.
- Durable async callback receipt replay now verifies that the stored `payload_digest` still matches
  the canonical callback payload before the receipt can participate in duplicate detection, event
  replay, or scheduler resume decisions.
- Durable async callback receipt replay also validates required identity, verifier, policy snapshot,
  and receipt timestamp metadata before the receipt can participate in duplicate detection.
- SQLite async callback receipt replay also validates that the receipt JSON operation/idempotency
  identity matches the durable row key, preventing a corrupted receipt from relocating duplicate
  detection to a different operation during recovery.
- SQLite async callback receipt replay now cross-checks receipt run, node, attempt, and provider
  operation metadata against the registered operation before duplicate handling can use it.
- SQLite async callback receipt replay now rejects duplicate artifact ids in artifact-backed
  callback receipts before duplicate handling can treat a corrupted row as an idempotent replay.
- `SqliteCallbackDeliveryQueue` now persists pending and retry-scheduled callback deliveries across
  reopen, preserving delivery status, idempotency keys, sequence ordering, and retry due times.
- SQLite callback delivery queue replay validates that each stored delivery JSON identity matches
  the durable delivery row key before it can be returned, recovered, cancelled, or delivered.
- SQLite callback delivery queue replay also validates row status, retry due time, and sequence
  metadata against the stored delivery JSON before those indexed columns can drive scheduling.
- SQLite callback delivery persistence now rejects `delivered` and `acknowledged` records whose
  delivery or acknowledgement timestamp is zero, preventing invalid audit times from entering the
  durable delivery queue.
- SQLite callback delivery persistence now rejects acknowledged records whose acknowledgement time
  precedes a recorded delivery time, preserving monotonic delivery audit timestamps across replay.
- SQLite callback delivery persistence also rejects retry-scheduled records whose
  `next_retry_at_unix_ms` is zero, keeping due-time indexes from treating malformed retries as
  immediately deliverable after worker recovery or replay.
- `CallbackSubscription` validation now rejects zero creation timestamps, so subscription
  registrations and public-field validation cannot produce replay or audit records without a real
  creation instant.
- `WebhookDeliveryWorker` now processes due durable callback deliveries with signed webhook
  envelopes, an injected transport boundary, and persisted success/retry outcomes. If the signed
  envelope exceeds the target payload limit, the worker records a terminal delivery failure with a
  payload-limit diagnostic and does not call the transport.
- `CallbackDeliveryProjection` now exposes a response-transition helper that applies classified
  webhook receiver responses to durable delivery state: 2xx marks delivered, 409 marks acknowledged,
  429/5xx schedule bounded retries, and retry exhaustion remains failed without over-scheduling.
- Callback delivery response transitions now reject late receiver responses once a delivery is
  already terminal at the runtime scheduler boundary, preventing delivered, acknowledged,
  dead-lettered, cancelled, or expired delivery records from being rewritten by delayed network
  outcomes.
- The Rust callback retry policy constructor and Python callback helper normalize zero delay
  settings to a positive delay floor, and scheduler retries use deterministic bounded jitter unless
  a receiver-provided `Retry-After` delay is present.
- Callback delivery projections now validate retry, delivery, and acknowledgement timestamps as
  ISO-8601 datetimes, reject acknowledgement timestamps that precede delivery timestamps, and
  reject `acknowledged_at` on any delivery whose status is not `acknowledged`.
  The optional `graphblocks-callbacks` projection mirrors the core facade by also requiring
  nonblank `last_error` values on failed, dead-lettered, cancelled, or expired deliveries.
- The Python `CallbackDelivery` facade now preserves retry-scheduled failed attempts by allowing
  `failed` deliveries to carry `next_retry_at` with a nonblank `last_error`, while delivered,
  acknowledged, dead-lettered, cancelled, and expired records remain non-retryable terminal
  projections.
- The optional `graphblocks-callbacks` projection now applies the same distinction: retry-scheduled
  failed deliveries are ignored by mandatory failure actions until retry exhaustion, but they still
  cannot consume late webhook responses because only `delivering` records may apply transport
  outcomes.
- Callback subscriptions can schedule cursor replay from the authoritative `ApplicationProtocolLog`
  while applying the same event filters and deterministic delivery/idempotency metadata as live
  projection. Replay scheduling now resolves the requested cursor against retained run events and
  schedules no webhook deliveries when the cursor is unknown or expired, avoiding accidental
  replay-from-beginning behavior.
- `ApplicationProtocolLog` now exposes retained-window replay with explicit `CursorExpired`
  semantics, including the requested cursor, nearest retained cursor, last cursor, and last
  sequence for reconnect/attach callers.
- Runtime and Python facade `ApplicationProtocolLog` append now reject duplicate replay cursors
  assigned to different events, preserving unambiguous cursor replay and client deduplication by
  cursor.
- Runtime and Python facade `ApplicationProtocolLog` instances are scoped to the run id established
  by the first appended event and reject later events from another run, preserving per-run sequence
  and replay authority for background attach/cursor semantics. The shared application-protocol TCK
  now exercises this mixed-run rejection path and records the expected `run_mismatch` append error.
  The PyO3 binding maps this runtime error to structured `run_mismatch` JSON with expected and
  actual run identifiers, keeping native and Python adapter diagnostics aligned.
- Runtime and Python facade `ApplicationProtocolLog` now treat duplicate event IDs as idempotent
  only when the full event matches the committed record; mutated replays are rejected as conflicts
  so a globally unique `event_id` cannot hide divergent payload, cursor, sequence, or metadata.
- `AttachToRun` replay now has a typed runtime result that either returns retained missed events
  and the live-stream cursor, or reports expired-cursor recovery metadata. Rust
  `ApplicationProtocolLog::attach_to_run_with_status(...)` can attach the current
  `RunStatusSnapshot` to expired-cursor recovery, and the runtime TUI projection applies that
  snapshot when it belongs to the same run.
- Webhook delivery targets now have default-deny endpoint validation for unsupported schemes,
  localhost, loopback, private RFC1918 ranges, link-local metadata addresses, and malformed hosts,
  with explicit host allowlisting for trusted development or private deployments.
- Callback configuration diagnostics now map unsigned webhook subscriptions to `GB6002` and unsafe
  webhook endpoint failures to `GB6011` for compiler/deployment reporting. Userinfo-bearing
  webhook URLs are rejected as a typed unsafe endpoint case so diagnostics can identify the
  unsupported userinfo component instead of collapsing it into a generic malformed URL. Compiler
  diagnostics now also flag decimal and hex numeric IPv4 callback hosts that resolve to forbidden
  internal addresses, matching runtime webhook egress validation; decimal loopback host rejection
  is now pinned in the shared compiler TCK. Compiler diagnostics now also reject webhook callback
  delivery configs whose `method` is not `POST`, matching the `WebhookDeliveryTarget` schema before
  deployment. Webhook callback delivery URLs with surrounding whitespace are rejected before
  endpoint safety checks so target identity is not silently normalized during compilation.
  Callback delivery `kind` values are validated against the specification's literal target union
  before webhook-specific diagnostics are evaluated.
- Callback subscriptions can now explicitly mark forbidden authoritative uses, and diagnostics
  report callback delivery used as a source of truth for run correctness, billing, quota, audit, or
  effect commit as `GB6004`, with shared compiler TCK coverage.
- Callback subscription diagnostics now report mandatory callback delivery without retry,
  dead-letter, or fallback policy as `GB6006`, with shared compiler TCK coverage. The Python
  compiler now treats explicit `retryPolicyRef`, `deadLetterPolicy`/`deadLetterRef`, or
  `fallbackPolicy`/`fallbackRef` declarations as satisfying that mandatory callback recovery
  requirement even when no separate `failurePolicy` field is present.
- Callback subscription diagnostics now report impossible ordered-delivery requests (`GB6012`) and
  retrying or mandatory callback failure policies without explicit dead-letter or fallback behavior
  (`GB6014`), with shared compiler TCK coverage. The Rust and Python compilers both recognize
  fallback policies or fallback refs as valid callback recovery behavior; the Rust runtime-core
  diagnostic model exposes the same distinction through explicit dead-letter behavior metadata on
  callback subscriptions.
- Webhook delivery targets now enforce the specification's default `262144` byte payload limit
  before signing delivery envelopes, and tests cover explicit small-limit rejection for oversized
  callback projections.
- Webhook delivery workers now persist a `delivering` state after signing and before invoking the
  transport adapter, so failover and operator inspection can distinguish in-flight attempts from
  merely pending deliveries.
- Callback delivery queues can now recover persisted in-flight deliveries after worker restart by
  requeuing them as due pending deliveries with an explicit recovery reason, preserving at-least-once
  delivery semantics without claiming exactly-once delivery.
- Callback delivery queues can now cancel pending deliveries for a revoked subscription while
  leaving in-flight deliveries unchanged, matching the subscription-revocation rule that active
  sends may finish but no queued work should be newly delivered.
- Runtime callback subscription construction now validates visibility filter literals against the
  specification's `client`, `operator`, `internal`, and `audit_only` set before storing a
  subscription projection. Run-subscription and callback-registration server paths reject event type,
  node id, operation id, visibility, and minimum-severity filter literals that only become valid
  after trimming whitespace.
- Python and Rust callback event filters now expose an authorization projection that intersects
  requested visibility with the subscriber's allowed visibility, so a callback subscription filter
  cannot widen access to operator/internal/audit-only events. Protocol-event filtering treats
  absent visibility as default client visibility after that projection, while malformed visibility
  values remain non-matching.
- Server callback registration and run event subscription paths now apply that visibility projection
  before replay or storage, so unauthorized event visibility cannot be obtained by requesting a
  broader callback filter.
- Runtime callback subscription construction now validates scope literals against the specification's
  `run`, `conversation`, `project`, `tenant`, and `deployment` set. The compiler and server reject
  scopes that only become valid after trimming whitespace, preserving the configured capability
  scope as an exact literal.
- Runtime event subscriptions and callback registrations now treat `subscription_id`, `run_id`,
  and `scope_id` as exact durable identities. Values that only become valid after trimming
  whitespace are rejected before replay, storage, revoke, or acknowledgement state can alias a
  different capability.
- Runtime callback subscription `failure_policy` values are now exact enum literals.
  Whitespace-wrapped values are rejected instead of being normalized into a different
  retry/dead-letter contract.
- Runtime callback subscription and registration `status` values are now exact enum literals for
  `active`, `paused`, `expired`, and `revoked`; whitespace-wrapped or unknown lifecycle states are
  rejected before subscription, revocation, replay, or delivery projection storage can depend on
  them.
- Run event subscription and callback registration replay cursors are now exact cursor tokens.
  Whitespace-wrapped `replayFromCursor` values are rejected before cursor replay so clients cannot
  silently attach to a different retained event boundary.
- Runtime callback subscription construction now validates typed webhook delivery targets so direct
  target construction cannot bypass non-empty URL checks.
- Runtime callback event filters now match both spec camelCase `nodeId`/`operationId` payload
  fields and legacy snake_case `node_id`/`operation_id` payload fields.
- Runtime run-scoped callback subscriptions now enforce `scope_id` against each application event's
  `run_id` before scheduling delivery.
- Webhook egress policy now validates DNS-resolved addresses before transport, rejecting public
  hostnames that resolve to loopback, private, link-local, metadata, or otherwise forbidden
  addresses unless the host is explicitly allowlisted. Empty DNS resolution results are treated as
  retryable transport failures and stop before request construction.
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
  runtime. If a callback body declares `operation_id`/`operationId`, it must match the
  `/callbacks/{operation_id}` endpoint binding before any receipt can be accepted. Callback
  submissions that declare a `run_id` must reference a retained run event stream and include both
  `node_id` and `attempt_id` fences before they are accepted; once an operation has an accepted
  run-node-attempt receipt, later callbacks for that operation cannot switch to a different run,
  node, or attempt. An operation that first accepts an unscoped callback receipt also cannot later
  become run-scoped, and a run-scoped operation cannot later accept unscoped receipts under the same
  operation id.
  Callback ingress rejects run-scoped receipts when the authoritative run projection is already
  terminal, so late callbacks cannot appear resumable or create new stored resume receipts; the
  server now records a separate `ServerAsyncCallbackRejection` projection with callback,
  idempotency, payload digest, verifying principal, policy snapshot, run/node/attempt,
  provider-operation identity, terminal status when applicable, artifact ids, reason, and receipt
  timestamp even for terminal-run rejections that never become accepted callback receipts. This supports payload-too-large,
  unknown-run, missing-fence,
  terminal-run, stale-attempt, node-mismatch, scope-mismatch, and idempotency-conflict rejection
  audit and inspection. Rejection projection identities, reasons, statuses, policy snapshots,
  verifier ids, and artifact ids are exact audit tokens and reject whitespace-normalized values
  before they can be stored or replayed.
  Public callback ingress can opt into anti-enumeration acknowledgements for unknown declared runs:
  the server records the same `unknown_run` rejection projection but returns a generic `202`
  acknowledgement instead of exposing run existence through a `404`.
  Endpoint operation-id mismatches are also projected as `operation_id_mismatch` rejections keyed
  by the route-bound operation id, so a mismatched body declaration cannot create an accepted
  receipt or shift audit records into a body-controlled operation bucket.
  The server route enforces a configurable inline callback payload limit, defaulting to the
  specification's `262144` bytes, before accepting or storing a callback receipt.
  Callback bodies may supply `payload_digest`/`payloadDigest`; ingress validates the declared
  digest against the canonical callback payload before accepting the receipt or projecting a
  route-bound operation-id mismatch rejection.
  Callback top-level aliases such as `operation_id`/`operationId`, `callback_id`/`callbackId`,
  `idempotency_key`/`idempotencyKey`, `policy_snapshot_id`/`policySnapshotId`,
  `payload_digest`/`payloadDigest`, `run_id`/`runId`, `node_id`/`nodeId`, and
  `attempt_id`/`attemptId` are accepted only when both spellings agree, preventing ambiguous
  callback identity, fencing, and digest metadata. If the callback body and idempotency header
  both declare an idempotency key, they must also agree before the request can be accepted or
  projected as a route-bound rejection. The preferred `GraphBlocks-Idempotency-Key` header and
  legacy `Idempotency-Key` header are also rejected when both are present with different values.
  Callback ingress identity and fencing values, including operation id, callback id,
  idempotency key, run id, node id, attempt id, provider operation id, verifier id, policy
  snapshot id, and payload digest, are exact tokens; values that only become valid after trimming
  whitespace are rejected before receipt storage, duplicate detection, or stale-attempt checks.
  Callback receipt timestamps are validated as ISO datetimes, and nested callback JSON payloads are
  deep-frozen at ingress so later caller mutation cannot corrupt stored callback receipts or
  idempotency comparisons. Accepted callback submission projections now also record the canonical
  payload digest, verifying principal, policy snapshot id, and callback artifact references, and
  duplicate acknowledgements return the same receipt metadata instead of reducing the callback to an
  idempotency key. Accepted and duplicate callback acknowledgements also project run, node, attempt,
  and provider-operation fences when present, matching the stored receipt identity. Callback artifact
  references are immutable JSON objects with non-empty
  `artifact_id` and `uri` fields; optional `media_type` and `checksum` fields are validated when
  present, artifact string fields reject whitespace-normalized values, optional `size_bytes` must be
  a non-negative integer, camelCase aliases are normalized at ingress, and artifact refs are
  included in idempotency conflict detection.
  The Rust `graphblocks-runtime-core` async operation journal now records the same payload digest,
  verifying principal, and policy snapshot metadata on `ExternalCallbackRejected` events, including
  idempotency-conflict rejections for mutated callback replays.
  Mandatory callback-delivery failure actions can now be applied to Rust run state: pause actions
  transition the run to `paused_callback_delivery` and return the callback-delivery wait reason,
  while fail actions transition the run to `failed`.
  The Rust webhook delivery worker now also returns terminal mandatory run actions from due
  delivery processing, including receiver failures and local signing failures such as oversized
  payloads, while preserving the existing attempt-count API for callers that only need delivery
  progress.
  Deployments can configure the server callback route to require an installed authentication hook
  before `SubmitAsyncCallback` will parse, validate, or store a callback receipt. When an installed
  authentication hook rejects a parseable async callback submission, the server now records an
  `authentication_failed` rejection projection with the same operation, callback, idempotency,
  digest, policy snapshot, and fence metadata used by other rejected receipts, while preserving the
  existing `401` response and avoiding accepted callback storage.
- `graphblocks-runtime-core` now rejects async-operation cancel and expire terminal transitions
  whose timestamps are zero or precede the operation submission timestamp. This keeps local
  cancellation/expiry controls from writing impossible terminal state while preserving the existing
  timeout behavior where expiry detection may occur after the configured callback deadline.
- `graphblocks-client` now exposes a `SubmitAsyncCallback` HTTP helper that posts authenticated
  callback receipts to `/callbacks/{operation_id}` with the required idempotency header and typed
  run/node/attempt/provider fences, so clients do not need to hand-assemble callback ingress
  requests. Accepted callback responses must echo the requested operation, callback id,
  idempotency key, canonical payload digest, and any requested run/node/attempt/provider fences
  before the client treats the callback receipt as durable.
- `graphblocks-client` also exposes a `GetRunStatus` HTTP helper for `/runs/{run_id}`, sharing the
  same run-id validation, authentication header handling, and JSON error mapping as attach/replay
  helpers.
- `graphblocks-client` now exposes `ListRuns`, `PauseRun`, `ResumeRun`, and `ExpireRun` HTTP
  helpers. The lifecycle helpers validate run ids and optional control fields before posting the
  server's run-control JSON contract, while `ListRuns` returns the event-derived run status array
  from the framework-neutral `GET /runs` route.
- `graphblocks-client` now exposes an `AttachToRun` HTTP helper that posts the last replay cursor
  and declared surface capabilities, returning replayed events through the same
  `ApplicationEvent` parser used by event-stream helpers.
- `graphblocks-client` now exposes a `DetachFromRun` HTTP helper that records client detachment
  with an optional reason while preserving the authoritative run event stream.
- `RunGraphCommand` now carries the invocation `responseMode` contract through the HTTP client,
  allowing callers to request `sync`, `accepted`, or `background` `InvokeGraph` responses instead
  of being limited to the default synchronous path. `RunGraphResponse` validates non-empty run
  identity and status plus JSON-object outputs, and preserves accepted/background run-handle links
  (`eventStream`, `websocket`, `cancel`) and `initialCursor`; accepted and background response
  objects must carry every handle field. The public response facade and HTTP client reject missing
  or blank durable handle fields and validate that `initialCursor` belongs to the returned run id
  with a non-negative sequence before callers persist or replay from it. Run graph responses,
  stream snapshots, attach snapshots, and subscription snapshots must echo the requested `runId`
  before the client will attribute replay metadata to that run, and replayed events in run-scoped
  responses must carry matching event metadata `runId` values. Outgoing run replay and
  acknowledgement cursor arguments are also validated as `<run_id>:<sequence>` before the HTTP
  request is sent.
  Run status, run control, and detach responses must echo the requested `runId` before the client
  exposes their payloads.
  `LocalGraphBlocksClient` rejects non-`sync`
  response modes instead of pretending in-process execution has durable background-run lifetime.
- `graphblocks-client` now exposes a `SubscribeEvents` HTTP helper that stores run-scoped event
  subscriptions with replay cursor, filter, delivery target, and failure-policy configuration, and
  parses replayed events through the shared event-stream parser.
- Run-scoped event subscriptions now persist the authenticated principal as the subscription owner
  and project that owner in subscription creation responses for policy and audit follow-up.
  `UnsubscribeEvents` rejects requests from a different authenticated principal or tenant before mutating the
  subscription or returning an idempotent revoke projection.
- `graphblocks-client` now exposes `UnsubscribeEvents` and `AckEvent` HTTP helpers for run-scoped
  subscriptions, preserving the server's idempotent revoke and event/cursor acknowledgement
  projections. `AckEvent` rejects acknowledgements from a different authenticated principal or tenant before
  recording delivery state for the subscription. Revoke and acknowledgement responses must echo
  both the requested `runId` and `subscriptionId` before the client accepts them.
- `graphblocks-client` now exposes `RegisterCallback` and `RevokeCallback` HTTP helpers for
  callback delivery registrations, including replayed run-scope events, delivery target config,
  failure policy, and dead-letter policy projection. Callback registration responses must echo the
  requested `scope` and `scopeId` before the client treats replayed events as a projection for that
  subscription, and run-scoped callback registration replays reject events from any other run. A
  run-scoped callback registration `replayFromCursor` is validated against the requested run scope
  before dispatch. Callback revoke responses must echo the requested `subscriptionId` before the
  client treats the registration as revoked.
- `graphblocks-server` now also exposes the framework-neutral `GET /runs/{run_id}`
  `GetRunStatus` route, deriving status, release id, replay cursor, timestamps, wait reasons, and
  active operation projection from the authoritative stored application events and accepted async
  callback submissions. Terminal run states suppress active callback wait projections so late
  callback receipts do not appear resumable after cancellation, expiry, failure, or policy stop.
  Runtime run status snapshots now reject terminal projections that still expose wait reasons or
  active operations, and reject duplicate active operation ids instead of silently collapsing them.
  Retained run events must include non-boolean integer `sequence` metadata and ISO-valid
  `occurredAt` metadata before they can be projected through `GetRunStatus` or `ListRuns`,
  preventing invalid replay cursors or empty `startedAt`/`updatedAt` snapshots from becoming
  client-visible status. Run-control pauses project operator, budget, policy, and callback-delivery
  wait reasons.
- Stored server application events are immutable snapshots; `/events`, attach/replay, subscription
  replay, and websocket snapshot responses thaw them back to plain JSON payloads. The
  `GET /runs/{run_id}/events` route honors a `cursor` query parameter for retained event replay,
  returning only events after that cursor plus `lastCursor` metadata. Malformed event cursors are
  rejected before retention lookup, retained event `sequence` metadata must be a non-boolean
  non-negative integer before replay cursor projection, and well-formed missing cursors return
  `CursorExpired` with nearest replay cursor, last cursor, and current `runStatus` recovery
  metadata.
  Runtime protocol events now require a non-empty replay cursor at construction time, preserving
  cursor-based replay and duplicate-tolerant attach semantics for the authoritative event stream.
  The Python `ApplicationProtocolLog` facade also rejects unresolved replay cursors instead of
  replaying from the beginning, keeping client/server re-exports aligned with the retained replay
  contract. Websocket stream snapshots reject retained events without non-boolean non-negative
  integer `sequence` metadata before deriving the current cursor.
- `graphblocks-server` now exposes the framework-neutral `GET /runs` `ListRuns` route using the
  same event-derived run status projection, keeping `POST /runs` reserved for `InvokeGraph`.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/attach`
  `AttachToRun` route, replaying stored events after a supplied cursor and returning explicit
  `CursorExpired` recovery metadata, including current `runStatus`, when the requested cursor is no
  longer retained. Attach cursors must belong to the target run and use a non-negative integer
  sequence; retained event `sequence` metadata must also be a non-boolean non-negative integer
  before attach replay projection, and malformed or wrong-run cursors are rejected before retention
  lookup. Attach capability declarations are validated against the specification's literal set and
  rejected if they only become valid after trimming whitespace.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/detach`
  `DetachFromRun` route, recording client detach projections while preserving the authoritative
  event stream and current run status. Stored detach projection records are immutable snapshots,
  detach timestamps are validated as ISO datetimes, and retained event `sequence` metadata must be
  a non-boolean non-negative integer before the detach `lastCursor` is recorded. Repeated detach
  requests from the same client are idempotent and return the first detach record. Detach client
  identifiers and optional reason text are rejected if they only become valid after trimming
  whitespace.
- `graphblocks-server` now exposes the framework-neutral `POST /runs/{run_id}/subscriptions`
  `SubscribeEvents` route, recording run-scoped event subscription projections and replaying
  retained matching events from the authoritative event stream after an optional cursor. Replay
  filters honor event type, visibility, node ID, operation ID, minimum severity, and
  `includeTerminalEvents` predicates. Visibility, node, and operation filters match the
  specification's top-level `visibility`, `nodeId`, and `operationId` event fields and legacy
  payload fields. Visibility filters validate the specification's `client`, `operator`,
  `internal`, and `audit_only` literals. Nested event filter and delivery configs are immutable
  snapshots and are thawed back to plain JSON for response payloads. Subscription and
  callback-registration replay also apply the subscriber principal visibility boundary before filter
  matching, so malformed or unauthorized event visibility cannot be promoted to client-visible
  replay output.
  Run-scoped subscription ids are single-assignment and cannot overwrite an existing active or
  revoked projection. Subscription replay cursors must belong to the subscribed run before retention
  lookup and must use a non-negative integer sequence. Retained event `sequence` metadata must also
  be a non-boolean non-negative integer before subscription replay projection. Expired subscription
  replay cursors return the same current `runStatus` recovery metadata as raw event replay and
  attach. Subscription and callback registration projections validate the spec failure policy
  literals before storage, and ordered delivery requests are rejected unless the target kind can
  preserve run ordering. Mandatory delivery
  projections cannot use best-effort failure handling unless an explicit dead-letter configuration
  is supplied, and projections that explicitly select `retry_then_dead_letter` must declare a
  dead-letter or fallback behavior before storage. Route validation rejects callback delivery
  projections that mark themselves as a source of truth. Subscription creation timestamps
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
  timestamp. Acknowledgement timestamps are validated as ISO datetimes, and acknowledgement cursors
  must belong to the target run and use a non-negative integer sequence before retained-event
  lookup. Retained event `sequence` metadata must also be a non-boolean non-negative integer before
  acknowledgement matching. When a request supplies both event id and cursor, both identifiers must
  resolve to the same retained event before an acknowledgement is recorded. Acknowledged events must
  also be visible to the subscription owner and match the active subscription's event filter, so
  malformed or unauthorized event visibility cannot be acknowledged through the control route.
- Server inspection accessors for callback receipts, callback rejections, late callbacks,
  detachments, run controls, subscriptions, and event acknowledgements now validate lookup ids as
  exact durable identities, rejecting values that would only match after trimming whitespace.
- `graphblocks-server` now exposes framework-neutral `POST /callbacks/register` and
  `DELETE /callbacks/{subscription_id}` `RegisterCallback`/`RevokeCallback` routes, storing
  callback delivery registration projections and replaying retained run-scoped matching events
  without making callback delivery authoritative. Callback registration validates the specification
  scope literals (`run`, `conversation`, `project`, `tenant`, `deployment`). Nested event filter and
  delivery configs are immutable snapshots and are thawed back to plain JSON for response payloads.
  For authenticated principals with a tenant claim, tenant-scope callback registrations are accepted
  only when `scopeId` matches the principal tenant.
  Authenticated registration requests persist the authorized principal as the callback owner and
  project that owner through the registration response for later policy/audit decisions.
  `RevokeCallback` rejects requests from a different authenticated principal or tenant before mutating the
  stored registration or returning an idempotent revoke projection.
  Callback registration ids are single-assignment and cannot overwrite an existing active or
  revoked projection. Repeating `RevokeCallback` for an already revoked registration is idempotent
  and does not rewrite the stored projection. Callback registrations share the same route-level
  ordered delivery, mandatory failure-policy, non-authoritative projection, retained event
  sequence validation during replay, and creation timestamp validation as run-scoped subscriptions;
  webhook delivery targets reject non-`POST` methods and whitespace-wrapped URLs before registration
  storage. Callback delivery kind, webhook method, and webhook signing algorithm literals are
  rejected if they only become valid after trimming whitespace.
  `pause_run_on_failure` and `fail_run_on_failure` are treated as mandatory failure policies and
  require configured dead-letter or fallback behavior via `deadLetterPolicy`/`deadLetterRef` or
  `fallbackPolicy`/`fallbackRef` fields.
- `graphblocks-server` now exposes framework-neutral `POST /runs/{run_id}/cancel`,
  `POST /runs/{run_id}/pause`, `POST /runs/{run_id}/resume`, and
  `POST /runs/{run_id}/expire` `CancelRun`/`PauseRun`/`ResumeRun`/`ExpireRun` routes,
  recording run-control projections and reflecting the latest control state in `GetRunStatus`
  while preserving the authoritative event stream. `CancelRun` projects terminal `cancelled`, and
  both cancelled and expired controls set `completedAt` in status snapshots. Stored run-control
  projection records are immutable snapshots with ISO-validated timestamps and authenticated actor
  principal snapshots, and `PauseRun` accepts `pauseKind` values `operator`, `budget`, `policy`,
  and `callback_delivery` to project the corresponding wait reason. Run-control reason text is
  rejected if it only becomes valid after trimming whitespace, preserving audit and duplicate
  comparison semantics.
  Runtime run status snapshots reject zero `started_at` or `updated_at` timestamps and retained
  events without non-boolean integer `sequence` or ISO-valid `occurredAt` metadata before exposing
  status to attach/replay, `GetRunStatus`, or `ListRuns` callers.
  Non-terminal controls cannot reopen terminal runs, and `ResumeRun` is admitted only when the
  current server projection is paused or waiting. Repeating the latest control state, including
  non-terminal pause/resume projections, is idempotent only when the reason matches and does not
  append another projection; a same-state duplicate with a different reason is rejected as a conflict.
  Once a resume control is projected, stale callback wait reasons are no longer exposed in
  `waitingOn`, although active operation ids remain visible for reconciliation.
- `graphblocks-server` `InvokeGraph` now honors `responseMode: accepted` and `background` by
  returning a durable run handle with event stream, `/ws` websocket, cancel route, and initial
  cursor while retaining authoritative run events for later attach/replay from that cursor.
  Incoming `responseMode`, `runId`, `responseId`, `releaseId`, `policySnapshotId`, and optional
  `turnId` values are exact non-empty fields; the server rejects surrounding whitespace instead of
  trimming identities before creating the authoritative event stream.
  `InvokeGraph` validates event `occurredAt` timestamps as ISO datetimes before storing run events.
  Server ingress timestamp validation now also rejects space-separated datetimes and compact
  timezone offsets such as `+0000` for run invocation, async callback submission, and callback
  registration request timestamps before those requests can project durable state. The shared
  server timestamp parser now also rejects values that only become valid after trimming whitespace,
  keeping callback receipt, callback rejection, subscription, and registration audit timestamps
  exact.
  Run identifiers are single-assignment at this boundary: a repeated `InvokeGraph` request for an
  existing retained `runId` returns conflict and cannot overwrite the authoritative event stream.
- `SubscribeEvents` and `RegisterCallback` server projections now have coverage for replay from
  the accepted/background run handle's initial cursor, so event subscriptions and callback
  registrations can attach from the beginning without treating the initial cursor as expired.
- `graphblocks-callbacks` is now cataloged as an optional pure-Python callback projection package
  with no default HTTP/WebSocket client dependency. Its initial facade projects webhook envelopes,
  required headers, and HMAC-SHA256 signing/verification helpers while keeping callback delivery
  non-authoritative relative to the event stream and runtime journals.
- The callback projection facade now validates webhook payloads as strict JSON before signing:
  object keys must be non-empty exact strings, non-finite numbers are rejected, payloads are
  stored as immutable JSON snapshots, and a deterministic fuzz-style test pins signature stability under key
  reordering and caller mutation. Webhook envelope sequence numbers reject booleans as non-integer
  protocol values, preserving unambiguous replay ordering for subscribers.
- `graphblocks-callbacks` also exposes receiver-side HMAC-SHA256 header verification with required
  GraphBlocks webhook header checks, duplicate case-insensitive header rejection, envelope identity
  checks, malformed timestamp rejection, and replay-window enforcement for local tools, tests, and
  embedded receivers. Header generation and direct HMAC signing validate custom timestamp overrides
  as ISO-8601 datetimes before exposing signing material, and single-secret verification validates
  verifier secret configuration before parsing inbound headers.
- `graphblocks-callbacks` now includes dependency-free retry/dead-letter projection helpers:
  bounded deterministic jittered backoff, immutable delivery projections, dead-letter conversion,
  and redrive records that preserve original delivery identity, idempotency key, and attempt
  history without creating application events. Direct retry scheduling validates the retry policy
  contract before reading scheduling fields, and dead-letter/redrive projections reject timestamp
  regressions relative to delivery and dead-letter records.
- `graphblocks-callbacks` now exposes a dependency-free webhook target safety helper for callback
  delivery adapters, rejecting unsupported schemes, userinfo URLs, localhost/metadata hosts, and
  loopback/private/link-local/reserved IP destinations, including decimal and hex numeric IPv4
  aliases, unless private targets are explicitly allowed by deployment policy. The helper also
  rejects surrounding whitespace before URL parsing so ambiguous pasted targets cannot be
  normalized into an allowed delivery endpoint.
- `graphblocks-callbacks` now provides callback payload projection helpers that canonicalize
  strict JSON payloads, keep bounded payloads inline with a digest, and require an `ArtifactRef`
  when payloads exceed the configured inline byte limit. Payload projections now require canonical
  `sha256:` digest shape and reject empty or whitespace-wrapped nested object keys before digesting,
  and artifact-reference projections reject inline payload content, preserving the invariant that
  large callback bodies are referenced rather than embedded in durable callback records. Inline
  projection payloads are immutable snapshots, while outbound webhook and receipt helpers thaw fresh
  plain JSON values for delivery callers. Artifact-reference projections also require positive
  original payload size metadata so omitted payloads cannot be confused with empty inline callbacks.
- `graphblocks-callbacks` now maps webhook receiver HTTP responses into delivery decisions:
  2xx delivered, 409 acknowledged duplicate, 410 gone, 429/5xx retry, and other 4xx terminal
  failure, including `Retry-After` parsing and policy max-delay capping for retry scheduling.
  Absolute `Retry-After` values that are already stale at receipt time, including manually
  constructed typed retry decisions, are ignored so bounded retry policy remains authoritative.
- `graphblocks-callbacks` HMAC helpers now support optional `GraphBlocks-Key-Id` emission and
  keyring verification so receivers can accept current and previous signing secrets during
  rotation while rejecting unknown key IDs. Keyring verification validates replay-window policy
  before key selection, rejects empty keyrings, and validates configured key IDs/secrets before
  parsing inbound headers, so invalid verifier configuration cannot be hidden by malformed headers
  or an unmatched key ID.
- Callback resume admission in `graphblocks-callbacks` now compares canonical identity digests over
  tenant, release, run, node, attempt, and operation fields instead of delimiter-joined strings, so
  colon-containing IDs cannot collide and stale callbacks cannot resume a newer attempt.
- `graphblocks-callbacks` now includes an in-memory receiver replay guard that records callback
  delivery/idempotency identity, accepts first deliveries, treats exact repeats as duplicates, and
  flags mutated idempotency-key, delivery-id, or subscription-event replays as conflicts. Restored
  replay records must be keyed by their idempotency key and are validated for the same identity
  conflicts before the guard is used. Replay record and incoming envelope digests must be canonical
  `sha256:` values, and replay decisions also enforce status/flag consistency, so accepted,
  duplicate, and conflict outcomes cannot carry contradictory duplicate/conflict booleans.
- `graphblocks-callbacks` now projects durable `ExternalCallbackReceived` receipt metadata from a
  verified callback envelope and bounded/artifact-backed payload projection, preserving callback,
  run, operation, node, attempt, idempotency, payload digest, verifier, and policy snapshot identity
  for journal-before-resume flows without making callback delivery the source of truth. Receipt
  projections validate canonical `sha256:` payload digest shape before matching the payload
  projection. The receipt factory can also bind the envelope to the runtime's expected run,
  release, and tenant identity and rejects mismatches before a durable receipt projection is
  produced.
- External callback receipt projection now also rejects operation id drift when the callback
  envelope carries `operation_id`, so authenticated callback envelopes cannot be re-bound to a
  different async operation during journal-before-resume projection.
- External callback receipt projection rejects explicitly `unauthenticated` verifier markers, so
  the Python callback facade cannot mint durable `ExternalCallbackReceived` metadata from an
  unauthenticated callback.
- External callback receipt projection now requires the callback envelope type to be
  `ExternalCallbackReceived`, preventing ordinary callback-subscription events from being promoted
  into async resume receipts.
- `graphblocks-callbacks` now exposes callback endpoint auth/reference projections for bearer,
  HMAC, mTLS, and OIDC callback ingress. Endpoint refs bind accepted schema, operation, run, node,
  attempt, release, tenant, and optional provider-operation identity into stable resume fences so
  stale callbacks cannot be confused with the current resumable operation. Endpoint refs also
  validate their callback ingress URL as an absolute HTTP(S) URL and reject embedded userinfo
  credentials before they can be used for resume admission.
- Callback endpoint auth projections now reject mixed credential material for the wrong auth kind,
  so a resumable callback endpoint has exactly one verifier boundary.
- Bearer callback endpoint auth now rejects whitespace-only tokens at validation time, so callback
  ingress cannot be configured with visually present but empty credentials.
- `graphblocks-callbacks` now evaluates callback resume admission by comparing a durable
  `ExternalCallbackReceived` receipt against the callback endpoint's tenant/release/run/node/
  attempt/operation fencing key and endpoint expiry, returning explicit admitted, expired, or stale
  decisions before any scheduler resume signal is represented.
- Callback resume admission has deterministic fuzz coverage over tenant, release, run, node,
  attempt, and operation identity mutations to protect the async callback path from stale-attempt
  and wrong-scope resume regressions.
- Callback resume admission now rejects `ExternalCallbackReceived` receipts whose
  `provider_operation_id` differs from an endpoint-pinned provider operation, preserving the
  external-provider fence even when run, node, attempt, and operation ids still match.
- Python async operation result projections now reject mappings for `artifacts`, `diagnostics`,
  `metrics`, `checks`, `usage`, and `external_effects` sequence fields instead of silently
  iterating object keys, preserving the callback-result contract that untrusted projection
  payloads are bounded arrays of JSON values or typed external effect records.
- `SqliteAsyncOperationStore` now serializes durable callback admission across load, idempotency
  evaluation, and persistence, with a concurrency regression test proving duplicate callback
  deliveries produce one resume winner and duplicate receipts for the remaining workers.
- Python `AsyncOperation` records now reject ambiguous waits that define both `callback_ref` and
  `polling_ref`, so each external operation has one authoritative completion path before the
  scheduler can enter `WAITING_CALLBACK` or `POLLING`.
- Python and Rust compilers now reject graph-authored async operation configs that define both
  callback and polling completion refs as `InvalidAsyncOperation`, catching ambiguous external
  completion authority before deployment.
- Python and Rust compilers now also reject graph-authored async waits that define both a bounded
  timeout and an explicit infinite-wait policy as `InvalidAsyncOperation`, so the runtime never has
  to choose between contradictory wait-bound semantics.
- `graphblocks-runtime-core` now keeps the callback wait expiration boundary through
  `CallbackReceived`, rejecting callback receipts that have no expiration even before scheduler
  resume is attempted.
- Async operation results now reject committed external-effect records that omit an idempotency
  key, preserving retry/cancellation audit semantics for effects that have already escaped the
  runtime boundary.
- The Python retry TCK runner now treats `cancelOnAttempt` as an integer-only fixture control and
  ignores booleans, so boolean JSON flags cannot accidentally cancel the first effect attempt.
- Callback submission validation now rejects zero `received_at_unix_ms` values before any
  `ExternalCallbackReceived` receipt can be journaled.
- Runtime callback endpoint validation now rejects zero expiration timestamps, so mutated endpoint
  references cannot silently create invalid callback admission windows.
- Runtime callback subscription validation now rejects expiration timestamps that are not after
  subscription creation, preserving durable replay/subscription lifetime ordering.
- The application-event TCK runner now validates streamed tool-result event sequence fixtures before
  event construction and rejects boolean/non-integer values instead of coercing them into protocol
  sequence numbers.
- The same application-event fixture validation now covers generation chunk sequences before
  `GenerationChunk` construction, keeping boolean JSON flags out of output-policy stream ordering.
- Output-policy decision fixtures in the application-event TCK now validate `acceptedThrough` as an
  integer-only optional sequence, preventing boolean acceptance windows from being normalized to
  sequence `1`.
- Application-event `OutputCutoff` fixtures now pass sequence fields through the `OutputCutoff`
  validator instead of coercing them, so malformed boolean cutoff bounds cannot enter replay or
  draft-retention state.
- `graphblocks-server` now exposes framework-neutral
  `POST /callbacks/deliveries/{delivery_id}/redrive` and
  `POST /callbacks/deliveries/{delivery_id}/dead-letter`
  `RedriveCallbackDelivery`/`MoveCallbackToDeadLetter` routes, recording operator and reason
  projections while leaving durable callback queue/dead-letter authority in the runtime layer.
  Stored control projections are immutable snapshots with ISO-validated request timestamps, so
  inspection callers cannot mutate redrive or dead-letter history after recording. Repeated
  dead-letter moves for the same delivery are idempotent and return the first terminal move;
  redrive requests remain repeatable operator actions. Authenticated control requests derive the
  audit operator from the authorized principal when the body omits it and reject body-supplied
  operators that do not match the authenticated principal. Body-supplied operator and reason values
  are rejected if they only become valid after trimming whitespace, and delivery route ids are
  validated as exact decoded identities before storage, preserving audit identity and
  redrive/dead-letter reason text.
- `graphblocks-client` now exposes HTTP helpers for callback delivery redrive and dead-letter
  moves. The helpers validate delivery id and reason locally, optionally accept an explicit
  operator, omit it when bearer authentication should let the server derive the operator, preserve
  bearer authentication, require response `deliveryId` to echo the requested delivery, and surface
  the idempotent duplicate dead-letter response unchanged.
- `graphblocks-callbacks` now treats non-retryable `failed` callback deliveries as terminal
  delivery projections, while retry-scheduled failed deliveries remain non-terminal retry records.
  Late webhook responses cannot mutate either case because response transitions require an
  in-flight `delivering` record.
- `graphblocks-callbacks` callback delivery projections now reject status/timestamp conflicts
  such as pending or delivering records that already have a delivered timestamp or non-retryable
  terminal deliveries that still carry a future retry timestamp, keeping retry metadata scoped to
  live retry attempts.
- Callback delivery success projections now require delivered/acknowledged timestamps at direct
  construction time, preserving an inspectable audit trail for accepted and duplicate webhook
  deliveries.
- `graphblocks-runtime-core` now validates callback delivery records before SQLite persistence and
  after replay, rejecting acknowledged or otherwise terminal deliveries that still carry retry
  timestamps.
- Callback dead-letter construction now rejects zero `dead_lettered_at_unix_ms` values, keeping
  durable redrive/audit projections tied to a real recorded timestamp.
- Callback dead-letter redrive now rejects zero `redriven_at_unix_ms` values and timestamps earlier
  than the dead-letter record, preventing operator redrive audit time regressions.
- Redriven callback delivery records now require non-empty operator and reason audit fields when
  `redrive_count` is positive, and non-redriven records cannot carry stray redrive audit metadata.
- Async callback ingestion now treats `provider_operation_id` as fenced operation metadata and
  rejects callbacks that omit or contradict the registered provider operation before journaling
  receipt or resuming the run.
- `graphblocks-runtime-core` and `graphblocks-callbacks` now reject explicitly `unauthenticated`
  async callback submissions/receipts before normal receipt journaling, artifact-backed callback
  compaction, or pre-operation quarantine can create resumable state.
- `graphblocks-server` now applies the same provider-operation fence at callback ingress for
  repeated receipts on one operation, producing a dedicated `provider_operation_mismatch`
  rejection before the generic duplicate-receipt path.
- `graphblocks-server` run-control projections now reject pause/resume/cancel/expire commands
  when the authoritative event stream has already reached a terminal `RunSucceeded`,
  `RunCompleted`, `RunFailed`, `RunCancelled`, `RunPolicyStopped`, or `RunExpired` event,
  preventing control projections from reopening completed application or protocol streams.
- `RunPolicyStopped` is now a first-class `ApplicationProtocolEventKind` and participates in the
  callback subscription terminal-event filter, so policy-stopped background runs can notify
  subscribers through the same projection path as completed, failed, or cancelled runs. The Python
  protocol facade exports the same event kind and continues to compare its event tuple against the
  shared application-protocol TCK. Rust TUI attach projections now also display `RunPolicyStopped`
  as an error row and advance the run view to `policy_stopped`; the Python TUI package maps the
  same protocol event to `policy_stopped` session status.
- `RunExpired` is now a first-class Rust `ApplicationProtocolEventKind`, is included in the shared
  application-protocol TCK, participates in callback terminal-event filtering, and advances Rust
  TUI attach projections to terminal `expired` state. The Python protocol facade, server terminal
  event handling, and Python TUI package mirror the same event.
- Server run-status projection now keeps terminal application/protocol events authoritative over
  stale pause/resume control projections, so a completed event stream cannot continue to appear
  paused or resumable in `GetRunStatus`/`ListRuns`.
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
- The Python `ApplicationEvent` facade now carries the authoritative event-stream metadata needed
  by callback projections and cursor replay: stable cursor, graph/node/operation ids, and typed
  client/operator/internal/audit-only visibility. Application command, protocol event, and
  application-event payload snapshots are now deep-frozen after construction, including nested
  object and array values, so authoritative replay records cannot be mutated by retained caller
  references.
- Python `ApplicationEventMetadata` and `ApplicationProtocolEventMetadata` now validate replay
  cursors, run/event/release identities, graph/node/operation routing fields, and event visibility
  as exact values, rejecting whitespace-wrapped identifiers before authoritative replay or callback
  filtering can store them.
- Python `ApplicationCommandMetadata` now applies the same exact-value validation to command ids,
  run ids, protocol versions, turn ids, and idempotency keys before control-plane commands can be
  routed, deduplicated, or audited.
- Application command, protocol event, and authoritative application event payloads now reject
  top-level and nested mapping keys with surrounding whitespace before freezing, so payload
  evidence cannot depend on consumer-specific key trimming.
- Python callback `EventFilter` now matches typed `ApplicationEvent` records and protocol events by
  event type, visibility, node id, operation id, severity floor, and terminal-event inclusion,
  treating absent protocol visibility as default client visibility while keeping malformed
  visibility hidden. This aligns the core schema facade with server-side callback subscription
  filtering.
- Rust `graphblocks-runtime-core::callback_delivery::EventFilter` now matches native
  `ApplicationEvent` records by canonical metadata visibility, node id, and operation id before
  falling back to legacy payload routing fields, so runtime callback routing can run before
  protocol projection without duplicating routing identity in payloads.
- `graphblocks-server` invocation-created run events now persist the same authoritative metadata
  envelope, including replay cursor and visibility, so accepted/background run attach and callback
  replay surfaces do not require metadata to be duplicated in payloads.
- `graphblocks-runtime-core::application_event::ApplicationEventMetadata` now mirrors the
  authoritative event-stream fields from the async/callback amendment: cursor, graph id, node id,
  operation id, and typed client/operator/internal/audit-only visibility.
- Rust signed webhook callback envelopes now include optional `operation_id` metadata when present
  on the source application-protocol event. The field is inserted before payload-limit evaluation
  and HMAC signing, so async operation identity participates in webhook delivery verification.
- Application-protocol event metadata now carries `release_id` in both Rust and Python TCK
  projections. Rust signed webhook callback envelopes include that release identity before
  payload-limit checks and HMAC signing, aligning callback delivery with the authoritative
  replayable event stream contract.
- Rust tool resolution now rejects empty entries in every scoped-capability dimension
  (`application_tools`, graph, principal, tenant/conversation/data policy, deployment, and budget)
  before resolving visible tools, matching the scoped-capability contract and Python facade
  validation.
- Python tool resolution scope now treats every scoped-capability item as an exact identity,
  rejecting whitespace-wrapped capability names before catalog resolution can silently widen or
  narrow the model-visible tool set.
- Python block and graph tool implementations now reject empty or whitespace-wrapped
  `input_mapping` and `output_mapping` entries, keeping argument/result projection contracts from
  aliasing different source or destination fields after implicit trimming.
- Rust tool admission now rejects expired `before_tool_or_effect` policy decisions before applying
  allow/deny effects, so stale policy approvals cannot admit a tool side effect after their
  `valid_until` boundary.
- Python tool admission now applies the same `PolicyDecision.valid_until` freshness gate as the
  Rust runtime before allowing a tool call, keeping the authoring/schema facade aligned with the
  normative admission semantics.
- Python tool admission now rejects space-separated datetimes and compact timezone offsets such
  as `+0000` in `admitted_at`, resolved-tool `valid_until`, and policy-decision `valid_until`,
  keeping freshness checks aligned with RFC 3339-style runtime timestamp parsing.
- The shared tool-lifecycle TCK now covers expired `before_tool_or_effect` policy decisions in
  both the Rust runtime runner and Python conformance runner, proving that stale decisions are
  rejected before approval or side-effect admission.
- Missing input schemas are covered by the shared tool-lifecycle TCK, proving that schema
  registration is mandatory before tool admission.
- Resolved-tool mismatches are covered by the shared tool-lifecycle TCK before schema or approval
  gates, proving that calls cannot be admitted against a different scoped capability.
- Tool-name mismatches are covered by the same TCK path, proving that a call cannot swap the
  model-visible tool contract while reusing a resolved capability id.
- Argument-digest mismatches are covered by the same TCK path, proving that changed tool
  arguments cannot reuse stale canonical hashes to reach schema, policy, approval, or effect gates.
- Expired resolved tools are covered by the same TCK path, proving that scoped tool capabilities
  cannot be admitted after their resolution window closes.
- The same shared tool-lifecycle TCK now covers policy input digest mismatches, proving that a
  decision for a different `before_tool_or_effect` request cannot be replayed into admission.
- Missing policy input digests are covered by the shared tool-lifecycle TCK as well, keeping
  unauditable policy decisions from reaching approval or side-effect admission.
- The tool-lifecycle TCK also covers explicit policy deny decisions, proving that runtime policy
  refusal stops admission before approval fallback or side-effect execution.
- Deferred policy decisions are covered by the same tool-lifecycle TCK path, so an unresolved
  `before_tool_or_effect` decision cannot be interpreted as runtime admission.
- Required approval is covered by the shared tool-lifecycle TCK before idempotency or execution
  gates, preserving the approval-bound-to-call contract for effectful tools.
- Expired approval records are covered by the shared tool-lifecycle TCK, proving that approval
  validity is rechecked at admission time rather than treated as a permanent grant.
- Python approval request and approval record metadata now deep-freeze nested mappings and
  sequences. Metadata keys and credential refs are exact values, so audit/review annotations
  attached to an approval cannot be mutated or whitespace-normalized after approval admission
  decisions are constructed.
- Python tool approval requests now validate approval ids, call ids, tool names, definition,
  binding, and argument digests, policy snapshots, principals, and approver ids as exact
  immutable identities, rejecting values that only become valid after trimming whitespace before an
  approval can authorize a tool-call revision.
- Required idempotency keys are covered by the shared tool-lifecycle TCK after approval succeeds,
  proving that state-changing tools still cannot admit without retry-safe operation identity.
- Blank idempotency keys are covered in the same TCK suite, so whitespace-only operation identity
  cannot satisfy retry or cancellation safety checks.
- Python tool admission now validates admission principals and optional/required idempotency keys
  as exact identities, so retry-safe operation keys that only become valid after trimming
  whitespace cannot authorize an admitted tool call.
- Rust `ApplicationEventVisibility` now exposes stable spec literals and parsing for
  client/operator/internal/audit-only event-stream projections.
- `graphblocks-client` now preserves authoritative event metadata from server replay/attach
  payloads, including cursor, graph/node/operation ids, and visibility.
- Client package protocol tests now assert the enriched server contract for callback receipts
  (payload digest, verifier, policy snapshot, run/node/attempt/provider fences) and constrained
  subscription visibility, rather than treating those server-owned fields as optional noise.
  `submit_async_callback` now validates callback payloads with the canonical JSON encoder before
  transport and rejects non-string or blank object keys, so `NaN`, non-serializable values, or
  key-coercing payload objects cannot be sent as callback content. The same client boundary rejects
  Python tuples before they can be serialized as JSON arrays.
  `subscribe_events` and `register_callback` apply the same canonical JSON and object-key checks to
  `event_filter` and `delivery` payloads before constructing HTTP requests.
  `RunGraphCommand` now applies the same canonical JSON and object-key checks to graph documents
  and input objects before local or HTTP execution, so malformed run invocations fail at the client
  boundary rather than being coerced by a downstream serializer.
  `RemoteToolInvocation` now parses caller-supplied `arguments_json` with strict JSON semantics,
  rejecting non-standard constants such as `NaN` before argument digest verification and again when
  request-contract projections replay stored invocation arguments.
  MCP and OpenAPI adapter invocations now apply the same strict parsing to caller-supplied
  `arguments_json`, so non-standard constants are rejected before the executable request contract is
  canonicalized, digest-checked, or replayed through request-contract projections.
  Remote-service, MCP, and OpenAPI adapter invocations now reject space-separated datetimes and
  compact timezone offsets such as `+0000` in resolved-tool `valid_until` and caller-supplied
  `validation_time` checks, keeping scoped capability expiry enforcement aligned with the runtime's
  RFC 3339-style timestamp contract while still accepting canonical offset timestamps such as
  `-05:00`.
  `graphblocks-policy-opa` and `graphblocks-policy-cedar` now parse stored PDP request contracts
  with strict JSON semantics, rejecting non-standard constants such as `NaN` before OPA input or
  Cedar authorization payloads are exposed to external policy engines.
  `graphblocks-client` now also parses HTTP response bodies as strict JSON, rejecting non-standard
  constants such as `NaN` before response payloads enter application-event or run-status models.
  `GraphBlocksServerApp` now routes all JSON request-body decoding through a strict parser, so
  non-standard constants such as `NaN` are denied at ingress before command-specific validation.
  `ServerResponse.json` now validates nested payload JSON values and uses strict serialization, so
  non-finite floats or key-coercing nested objects cannot be emitted in server responses.
  `HttpGraphBlocksClient` now URL-encodes run, operation, subscription, callback, and delivery ids
  before inserting them into HTTP path segments, so protocol identifiers containing URL syntax stay
  within their intended route component. `GraphBlocksServerApp` decodes those encoded path
  parameters at route-match time, so handlers receive the authoritative identifier rather than a
  transport-escaped surrogate. `HttpGraphBlocksClient.run_events` now accepts a cursor query
  parameter for replaying `/runs/{run_id}/events` from a retained event-stream position. Accepted
  and background run handles now encode run ids in their returned event-stream, websocket, and
  cancel route links while preserving the canonical run id and initial cursor values.
  CLI JSON ingress now uses the same strict parsing for `run --input-json`, native runtime result
  decoding, GraphRelease bundle verification, and Kubernetes deploy-plan rendering.
- `LocalGraphBlocksClient` now emits deterministic `run_id:sequence` cursors on its local
  application events, matching the replay contract used by server attach/event-stream routes.
- `graphblocks-testing` now preserves and reports authoritative event metadata when running
  application-event TCK cases, so cursor replay and callback-projection conformance fixtures can
  assert cursor, graph/node/operation ids, and visibility directly.
- `graphblocks-testing` now loads every JSON TCK fixture with strict JSON semantics, rejecting
  non-standard constants such as `NaN` before fixture data can become conformance evidence.
- `graphblocks-testing` durable callback projection cases now reject failed, dead-lettered,
  cancelled, or expired delivery rows that omit nonblank `lastError` evidence, so retry and
  dead-letter conformance cannot be proven from malformed failure records.
- Durable callback projection terminal diagnostics now report the actual terminal delivery status
  (`failed`, `dead_lettered`, `cancelled`, or `expired`) when `lastError` evidence is missing,
  preventing cancelled or expired rows from being misreported as failed evidence.
- Durable callback projection TCK cases now reject empty `deliveries` arrays, so callback delivery
  conformance cannot pass in either Rust or Python without at least one durable
  `CallbackDelivery` row.
- Durable callback projection TCK cases now reject non-object delivery entries before evaluating
  retry, duplicate, idempotency, or redrive evidence, preserving the `CallbackDelivery` envelope
  shape across the shared Rust and Python conformance harnesses.
- Durable callback projection TCK diagnostics now preserve original delivery-array indexes after
  non-object entries, so malformed rows still point to stable repair paths in replay fixtures.
- Durable callback projection TCK cases now have shared expected-diagnostic coverage rejecting
  delivery rows without nonblank `idempotencyKey` evidence, preserving the callback protocol's
  at-least-once deduplication contract in shared MVP fixtures.
- Durable callback projection TCK cases now have shared expected-diagnostic coverage rejecting
  `idempotencyKey` reuse across distinct subscription/event deliveries, preventing one receiver
  deduplication token from proving multiple callback delivery identities.
- Durable callback projection TCK cases now allow duplicate delivery attempts for the same
  subscription/event to reuse the receiver `idempotencyKey`, while still rejecting reuse across
  distinct logical deliveries.
- Durable callback projection TCK cases now reject delivery rows with missing callback delivery
  identity or non-integer sequence metadata, so replay, deduplication, and redrive assertions are
  grounded in valid `CallbackDelivery` envelopes.
- Durable callback projection fixtures now carry and validate `subscriptionId`, cursor, and
  delivery-attempt metadata, and shared TCK fixtures now reject missing `subscriptionId`, missing
  cursor, missing or non-integer `sequence`, and missing or non-integer `attempt` evidence,
  aligning callback conformance with the protocol's at-least-once delivery envelope.
- Durable callback projection shared TCK fixtures now require delivery `attempt` values to be
  positive integers, preserving the one-based callback delivery attempt history used by retry,
  dead-letter, and redrive projections.
- Durable callback projection shared TCK fixtures now require delivery `sequence` values to be
  positive integers, preserving replayable per-run event ordering evidence for callback delivery
  rows.
- Durable callback projection shared TCK fixtures now reject delivery rows without nonblank
  `deliveryId`, `eventId`, or `runId` evidence, so callback redrive, dead-letter, deduplication,
  run ownership, and audit assertions always have stable identities in Rust and Python.
- Durable callback projection shared TCK fixtures now reject duplicate nonblank `deliveryId`
  values, preserving the protocol requirement that each `CallbackDelivery` row has a globally
  unique delivery identity.
- Durable callback projection shared TCK fixtures now validate explicit `subscription` envelopes,
  including nonblank `subscriptionId`, protocol-defined `failurePolicy`, and boolean `mandatory`,
  and reject mandatory subscriptions with omitted or `best_effort` failure policy, so
  subscription-level delivery policy evidence cannot be silently ignored by Rust or Python.
- Durable callback projection shared TCK fixtures now include expected-diagnostic coverage for
  missing or blank subscription envelope `subscriptionId`, keeping Rust and Python aligned on
  `CallbackSubscription` identity validation.
- Durable callback projection shared TCK fixtures now reject explicitly empty subscription
  envelopes rather than treating them as absent optional data.
- Durable callback projection shared TCK fixtures now have explicit expected-diagnostic coverage
  for missing or non-boolean `mandatory` subscription flags, preserving the `CallbackSubscription`
  schema contract in the MVP callback protocol.
- Durable callback projection shared TCK fixtures now reject delivery rows whose `subscriptionId`
  differs from the explicit subscription envelope, ensuring retry, duplicate, and redrive evidence
  belongs to the projected subscription in both Rust and Python.
- Durable callback projection redrive evidence now requires an explicit `redrive` object before
  asserting `deadLetterPreservesEventId`; shared fixtures can express that requirement through
  `redriveAssertions`, preventing absent event ids from comparing equal and proving redrive
  preservation accidentally.
- Durable callback projection shared TCK cases now reject malformed `redriveAssertions` values
  when present in both Rust and Python runners, so assertion evidence cannot be silently discarded
  by treating non-object data as an empty assertion block.
- Durable callback projection shared TCK cases now require `redriveAssertions` fields to be real
  booleans in both Rust and Python runners, so string assertion values cannot silently disable
  required redrive evidence checks.
- Durable callback projection redrive evidence now also requires an explicit `redrive` object
  before asserting `redriveCreatesApplicationEvent`, so redrive application-event behavior cannot
  be proven from the projection default.
- Durable callback projection cases now have shared expected-diagnostic coverage rejecting delivery
  rows whose `status` is not one of the protocol-defined `CallbackDelivery.status` terminal or
  in-flight values, preventing arbitrary strings from satisfying callback-delivery TCK evidence.
- Durable callback projection retry and duplicate evidence now has shared expected-diagnostic
  coverage requiring typed integer `receiverStatus` values in the HTTP status-code range,
  preventing string coercion or non-HTTP codes from proving webhook retry or
  duplicate-acknowledgement behavior.
- Durable callback projection receiver evidence now requires 2xx webhook responses to be recorded
  as `delivered` or `acknowledged`, preventing successful receiver acknowledgements from being
  represented as failed delivery rows.
- Durable callback projection retry evidence now treats HTTP 429 receiver responses as retryable
  alongside 5xx responses, so `retry_then_dead_letter` rows cannot omit durable `nextRetryAt`
  metadata after rate limiting.
- Durable callback projection retry evidence now reports a distinct
  `retryScheduledAfterRetryableStatus` observation for 429-or-5xx retry rows while preserving the
  legacy `retryScheduledAfter5xx` observation for 5xx-specific cases.
- Durable callback projection retry evidence now has shared expected-diagnostic coverage rejecting
  blank, non-string, or unparsable `nextRetryAt` timestamps before considering a 429/5xx delivery
  as retry-scheduled, keeping webhook retry evidence tied to durable retry metadata.
- Durable callback projection success evidence now has shared expected-diagnostic coverage rejecting
  blank, non-string, or unparsable `deliveredAt` timestamps before accepting delivered or
  acknowledged callback delivery rows.
- Durable callback projection status/timestamp evidence now has shared expected-diagnostic coverage
  rejecting pending or delivering callback delivery rows that already expose `deliveredAt`, keeping
  in-flight delivery records distinct from accepted webhook delivery outcomes, and rejecting
  `acknowledgedAt` on rows whose status is not `acknowledged`, keeping duplicate acknowledgement
  timestamps scoped to duplicate-accepted delivery rows.
- Durable callback projection duplicate-acknowledgement evidence now has shared
  expected-diagnostic coverage rejecting blank, non-string, or unparsable `acknowledgedAt`
  timestamps before accepting duplicate webhook acknowledgement rows.
- Durable callback projection non-retryable 4xx evidence now has shared expected-diagnostic
  coverage requiring failed delivery rows to record `lastError: non_retryable`, preventing retry
  or receiver-error labels from proving terminal 4xx handling.
- Durable callback projection terminal failure evidence now has shared expected-diagnostic coverage
  requiring `lastError` on `failed`, `dead_lettered`, `cancelled`, and `expired` delivery rows.
- Durable callback projection subscription-gone evidence now has shared expected-diagnostic
  coverage requiring cancelled 410 delivery rows to record `lastError: subscription_gone`.
- Durable callback projection retry evidence now has shared expected-diagnostic coverage requiring
  `retry_then_dead_letter` subscriptions to include `nextRetryAt` on failed 429/5xx deliveries,
  ensuring retry scheduling is proven from the subscription's failure policy rather than inferred
  from status alone.
- Durable callback projection retry evidence now has shared expected-diagnostic coverage requiring
  429/5xx retry-scheduled deliveries to remain in `failed` status, preventing delivered or
  acknowledged rows from proving retry behavior.
- Durable callback projection terminal-state evidence now has shared expected-diagnostic coverage
  rejecting `nextRetryAt` on delivered, acknowledged, dead-lettered, cancelled, or expired
  deliveries, keeping terminal delivery rows non-retryable.
- Durable callback projection duplicate evidence now requires receiver `409` rows to be in
  `acknowledged` status, preventing duplicate-acknowledgement behavior from being proven by merely
  delivered rows.
- Durable callback projection delivery evidence now has shared expected-diagnostic coverage
  requiring parseable `deliveredAt` for `delivered`/`acknowledged` rows and parseable
  `acknowledgedAt` for acknowledged duplicate rows, including an explicit missing-`deliveredAt`
  acknowledged-delivery case, while rejecting acknowledgements that precede delivery time, aligning
  the shared TCK projection with durable callback terminal timestamp invariants.
- Durable callback projection shared TCK cases now require calendar-valid delivery timestamps in
  both Rust and Python runners, so impossible dates such as an invalid `nextRetryAt` day cannot
  satisfy retry, delivery, or acknowledgement evidence even when their text shape resembles an RFC
  3339 timestamp.
- Durable callback projection shared TCK cases now reject zero-year `nextRetryAt` retry timestamps
  in the Rust runner, preserving RFC 3339-style year bounds before accepting retry evidence.
- Durable callback projection shared TCK cases now reject `nextRetryAt` retry timestamps that use a
  space instead of the RFC 3339 `T` separator in the Python runner, keeping retry scheduling
  timestamp validation aligned with the Rust runner before accepting retry evidence.
- Durable callback projection shared TCK cases now reject compact timezone offsets such as `+0000`
  on `nextRetryAt` retry timestamps, so Python cannot accept retry scheduling evidence that the
  Rust TCK parser treats as non-RFC3339.
- Durable callback projection shared TCK cases now reject delivered-row `deliveredAt` timestamps
  that use a space instead of the RFC 3339 `T` separator in the Python runner, keeping terminal
  delivery timestamp validation aligned with the Rust runner before accepting delivery evidence.
- Durable callback projection shared TCK cases now reject compact timezone offsets such as `+0000`
  on delivered-row `deliveredAt` timestamps, so Python cannot accept terminal delivery evidence
  that the Rust TCK parser treats as non-RFC3339.
- Durable callback projection shared TCK cases now reject acknowledged-row `acknowledgedAt`
  timestamps that use a space instead of the RFC 3339 `T` separator in the Python runner, keeping
  duplicate acknowledgement timestamp validation aligned with the Rust runner before accepting
  acknowledgement evidence.
- Durable callback projection shared TCK cases now reject compact timezone offsets such as `+0000`
  on acknowledged-row `acknowledgedAt` timestamps, so Python cannot accept duplicate acknowledgement
  evidence that the Rust TCK parser treats as non-RFC3339.
- Durable callback projection shared TCK cases now require nonblank redrive operator and reason
  audit metadata in both Rust and Python runners, so dead-letter redrive conformance preserves the
  operator action context required for durable audit and redrive review.
- Durable callback projection shared TCK cases now reject non-object redrive entries when present,
  so malformed dead-letter redrive projections cannot be silently treated as absent optional data
  by either Rust or Python.
- Durable callback projection shared TCK cases now require nonblank redrive `deliveryId`, `eventId`,
  and `originalEventId` fields in both Rust and Python runners, preventing missing identity values
  from satisfying dead-letter preservation checks by comparing absent data.
- Durable callback projection shared TCK cases now reject mismatched redrive `eventId` and
  `originalEventId` values in both Rust and Python runners, making event identity preservation a
  mandatory redrive invariant instead of an optional observed flag.
- Durable callback projection shared TCK cases now require redrive `createsApplicationEvent`
  evidence to be present and a real boolean in both Rust and Python runners, preventing absent
  evidence or truthy string values from satisfying the no-duplicate-event redrive invariant.
- Durable callback projection shared TCK cases now require fixture-level outage evidence
  `nonMandatoryOutageBlocksRun` to be present and a real boolean in both Rust and Python runners,
  preventing absent evidence or string coercion from satisfying non-mandatory webhook outage
  behavior.
- Durable background-run shared TCK cases now require a real boolean `detach.cancelRun` value in
  both Rust and Python runners before proving that background/job lifetimes outlive client detach,
  preventing truthy strings from satisfying run-lifetime behavior.
- Durable background-run shared TCK cases now require `lifetime` to be exactly `background` or
  `job` in both Rust and Python runners, preventing client-bound or coerced lifetimes from
  satisfying detach-survival assertions.
- Durable background-run shared TCK cases now require a real boolean
  `attach.summaryOnExpiredCursor` value in both Rust and Python runners before proving compacted
  summary delivery for expired cursors.
- Durable background-run shared TCK cases now require present cursor fields (`attach.lastCursor`,
  `attach.expiredCursor`, and `retention.retainedFromCursor`) to be nonblank strings in both Rust
  and Python runners before they can drive replay or cursor-expiry assertions.
- Durable background-run shared TCK cases now require an object `initialResponse` carrying
  nonblank `runId`/`run_id`, `eventStream`/`event_stream`, `websocket`/`web_socket`,
  `cancel`/`cancel_route`, and `initialCursor`/`initial_cursor` values in both Rust and Python
  runners, require `status` to match the declared response mode, and require the event-stream,
  websocket, and cancel handles to encode the returned run id and correct endpoint kind, preventing
  truthy placeholder values, missing websocket or cancellation links, or mismatched run links from
  satisfying durable response and replay-handle semantics.
- Durable background-run shared TCK cases now require `responseMode`/`response_mode` to be exactly
  `accepted` or `background` in both Rust and Python runners, and apply the durable run-id handle
  requirement to background responses as well as accepted responses.
- Durable background-run shared TCK cases now reject non-object event-stream entries in both Rust
  and Python runners instead of silently dropping them before replay/cursor assertions.
- Durable background-run shared TCK cases now require nonblank event `eventId`/`event_id`,
  nonblank `runId`/`run_id`, nonblank `releaseId`/`release_id`, present `payload`, nonblank
  `cursor`, nonblank `type`, ISO-valid `occurredAt`/`occurred_at`, and strictly increasing
  positive integer `sequence` fields in both Rust and Python runners, require event run ids to
  match the returned run id when available, validate declared event `visibility` against the spec
  literals, require optional declared event metadata ids (`graphId`/`graph_id`,
  `nodeId`/`node_id`, `turnId`/`turn_id`, and `operationId`/`operation_id`) to be nonblank
  strings when present, reject duplicate event ids or replay cursors, and forbid event cursors from
  reusing the initial run cursor before proving cursor replay, preventing anonymous, cross-run,
  payload-less, duplicate, ambiguous, unauthorized-visibility, malformed-metadata, untyped,
  cursorless, untimestamped, or unordered events from satisfying replay assertions.
- Durable background-run shared TCK cases now reject calendar-invalid event `occurredAt` values in
  the Rust runner, aligning background event replay timestamp validation with the Python runner
  before accepting cursor replay evidence.
- Durable background-run shared TCK cases now reject zero-year event `occurredAt` values in the
  Rust runner, preserving RFC 3339-style year bounds before accepting replay evidence.
- Durable background-run shared TCK cases now reject event `occurredAt` timestamps that use a space
  instead of the RFC 3339 `T` separator in the Python runner, keeping background replay timestamp
  validation aligned with the Rust runner before replay evidence is accepted.
- Durable background-run shared TCK cases now reject compact timezone offsets such as `+0000` on
  event `occurredAt` timestamps, so Python cannot accept background replay evidence that the Rust
  TCK parser treats as non-RFC3339.
- Durable background-run shared TCK cases now expose `diagnosticCount` on successful event-stream
  projections and include a valid fractional-second event `occurredAt` fixture, so Rust and Python
  both accept RFC 3339-style replay event timestamps without hidden structural diagnostics.
- Durable background-run shared TCK cases now prove attach replay from the initial run-handle
  cursor or the matched event cursor position, and cursor expiry from the retained-boundary
  cursor's position, in the authoritative event stream rather than from lexicographic cursor
  ordering, so opaque replay tokens such as `evt_10` cannot be skipped after `evt_2`.
- Durable background-run shared TCK cases now require `sourceOfTruth`/`source_of_truth` to be
  exactly `ApplicationEventStream` in both Rust and Python runners, preventing callback
  subscriptions or truthy placeholders from satisfying the authoritative-event-stream invariant.
- Durable async callback resume-guard shared TCK cases now require real boolean values for the
  authentication, schema, timeout, cancellation, stale-attempt, source-event, and provider-mismatch
  safety checks in both Rust and Python runners, rejecting missing guard fields as well as truthy
  strings before they can satisfy callback resume conformance.
- Durable async callback resume-guard shared TCK cases now require `operation.deadline` to be a
  calendar-valid timestamp in the Rust runner, so digit-shaped but impossible deadlines cannot
  satisfy callback timeout and resume-admission evidence.
- Durable async callback resume-guard shared TCK cases now reject malformed deadline separators and
  impossible month/day combinations in the Rust runner, keeping callback timeout and resume
  admission timestamp parsing aligned with the Python TCK runner.
- Durable async callback resume-guard shared TCK cases now reject zero-year operation deadlines in
  the Rust runner, preserving RFC 3339-style year bounds before callback resume admission.
- Durable async callback resume-guard shared TCK cases now reject operation deadlines that use a
  space instead of the RFC 3339 `T` separator in the Python runner, keeping deadline parsing aligned
  with the Rust runner before callback resume admission.
- Durable async callback resume-guard shared TCK cases now reject compact timezone offsets such as
  `+0000` in operation deadlines, so Python's permissive `datetime.fromisoformat` behavior cannot
  accept non-RFC3339 callback timeout evidence that Rust rejects.
- Durable async callback resume-guard shared TCK cases now reject compact timezone offsets in
  callback `receivedAt` timestamps as well, keeping journal-before-resume receipt evidence aligned
  between the Python and Rust TCK runners.
- Durable async callback resume-guard shared TCK cases now also reject calendar-invalid callback
  `receivedAt` timestamps in the Rust runner, so invalid receipt times cannot satisfy
  journal-before-resume or timeout evidence.
- Durable async callback resume-guard shared TCK cases now reject callback `receivedAt` timestamps
  that use a space instead of the RFC 3339 `T` separator in the Python runner, keeping receipt
  parsing aligned with the Rust runner before journal-before-resume evidence is accepted.
- Durable async callback resume-guard shared TCK cases now expose `diagnosticCount` on success
  projections and include a valid fractional-second `receivedAt` receipt case, so Rust and Python
  both prove callback resume admission accepts RFC 3339-style fractional timestamps without hidden
  structural diagnostics.
- Durable async callback resume-guard shared TCK cases now require integer callback
  `journalSequence`, `resumeSequence`, and `successfulResumeCount` values in both Rust and Python
  runners before proving journal-before-resume and coordinator failover invariants, rejecting
  missing fields as well as string coercion from satisfying durable ordering evidence.
- Durable async callback resume-guard shared TCK cases now require callback `journalSequence` and
  resume `resumeSequence` values to be positive integers, preventing zero sentinels from proving
  callback receipt was durably journaled before run resume.
- Durable async callback resume-guard shared TCK cases now require `successfulResumeCount` to be
  exactly `1`, so coordinator failover and duplicate callback handling cannot claim more than one
  scheduler resume winner.
- Durable async callback resume-guard shared TCK cases now reject resume sequencing that is not
  strictly after the callback receipt journal sequence, making the journal-before-resume rule a
  structural diagnostic instead of only a failed observation.
- Durable async callback resume-guard shared TCK cases now require `reevaluates` to be a sequence
  of nonblank strings in both Rust and Python runners before proving policy, budget, and release
  compatibility re-evaluation, so missing lists, object keys, or malformed entries cannot satisfy
  resume-admission conformance.
- Durable async callback resume-guard shared TCK cases now require valid `reevaluates` sequences to
  include policy, budget, and release compatibility checks, preventing partial resume
  re-evaluation evidence from satisfying the callback protocol.
- Durable async callback resume-guard shared TCK cases now require valid `reevaluates` sequences to
  include `idempotency`, so callback resume conformance proves idempotency-state reevaluation as
  required by the async callback protocol.
- Durable async callback resume-guard shared TCK cases now require `budgetExhaustionState` to be
  `paused_budget` before budget-exhausted callback receipts can satisfy resume-admission
  conformance.
- Durable async callback resume-guard shared TCK cases now reject supplied operation envelopes with
  an invalid `kind`, keeping callback-resume evidence aligned with the `AsyncOperation` kind set
  already enforced by late external-operation reconciliation.
- Durable async cancel-race shared TCK cases now require real boolean values for callback receipt,
  resume-attempt, result-commit, and usage-reconciliation flags in both Rust and Python runners,
  rejecting missing flags as well as truthy strings before they can prove cancellation/resume race
  behavior.
- Durable async cancel-race shared TCK cases now require `race.winner` to identify `cancel`, so
  callback/cancellation races cannot satisfy conformance when the callback path is declared the
  winner after cancellation.
- Durable async cancel-race shared TCK cases now require explicit cancel journal evidence for
  cancel-winner races, so a callback-only journal cannot prove cancellation blocked resume.
- Durable async cancel-race shared TCK cases now require callback receipt journal evidence whenever
  `callbackReceiptRecorded` is true, so race summaries cannot claim a recorded callback without an
  `ExternalCallbackReceived` entry.
- Durable async cancel-race shared TCK cases now reject late result commits after a cancel-winning
  race, so callbacks that arrive after cancellation cannot satisfy conformance while committing a
  stale operation result.
- Durable async cancel-race shared TCK cases now reject resume attempts after a cancel-winning race,
  so a late callback cannot restart cancelled work even when the callback receipt is journaled.
- Durable async cancel-race shared TCK cases now require late usage reconciliation after a
  cancel-winning race, so cancelling work cannot skip UsageLedger accounting for late callback or
  provider usage.
- Durable async cancel-race shared TCK cases now require integer journal entry `sequence` values in
  both Rust and Python runners before proving cancel-before-callback ordering, preventing string
  coercion from satisfying race ordering conformance.
- Durable async cancel-race shared TCK cases now require journal entry `sequence` values to be
  positive integers, preventing zero sentinels from satisfying cancellation/callback race ordering
  evidence.
- Durable async cancel-race shared TCK cases now require a callback receipt sequence to be after
  the cancel sequence when `race.winner` is `cancel`, turning cancel-before-callback ordering into
  a structural diagnostic instead of only a failed observation.
- Durable async cancel-race shared TCK cases now reject non-object journal entries in both Rust and
  Python runners before evaluating cancel/callback ordering, preventing placeholder rows from being
  silently dropped from race evidence.
- Durable async cancel-race shared TCK cases now require nonblank, stable `ownershipFence` values
  across journal entries in both Rust and Python runners before accepting race-order evidence.
- Durable external-operation reconciliation shared TCK cases now require real boolean values for
  late callback commit/diagnostic/artifact-reference flags and late usage reconciliation in both
  Rust and Python runners, rejecting missing flags as well as truthy strings before they can prove
  cancellation, payload extraction, or billing reconciliation behavior. The same path rejects
  `lateCallback.commitsResult: true`, preserving the rule that late callbacks after terminal
  operation cancellation/expiry do not commit durable results, rejects
  `lateCallback.diagnosticRecorded: false` so late callback handling remains auditable, and rejects
  `lateCallback.payloadConvertedToArtifactRef: false` so untrusted late callback payload evidence
  remains artifact-backed. It also rejects `usage.reconciled: false`, preserving late provider
  usage reconciliation even after cancellation or expiry.
- Durable external-operation reconciliation shared TCK cases now require `operation.effectState` to
  be `committed` in both Rust and Python runners before accepting side-effect preservation evidence.
- Durable external-operation reconciliation shared TCK cases now also require
  `operation.effectJournaled` to be a real `true` boolean before accepting side-effect preservation
  evidence, tying late cancellation reconciliation to an auditable EffectJournal record.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `operation.operationId`, `operation.idempotencyKey`, `operation.runId`, `operation.nodeId`, and
  `operation.attemptId` values, an AsyncOperation `operation.resumeTokenHash` SHA-256 digest,
  nonblank `operation.expectedSchema` evidence, an ISO `operation.createdAt` timestamp, a valid
  AsyncOperation `operation.kind` literal, an ISO `operation.submittedAt` timestamp, a bounded-wait
  ISO `operation.expiresAt` timestamp, and a valid AsyncOperation `operation.state` literal in both
  Rust and Python runners before accepting late side-effect, callback, or usage reconciliation
  evidence. The Rust runner now parses those timestamps as calendar-valid instants instead of
  accepting digit-shaped dates with impossible months or days, and rejects lowercase `z` timestamp
  suffixes so Rust and Python agree on the accepted UTC designator. The same TCK path rejects
  non-terminal operation states before late reconciliation, because late callback, effect, and
  usage reconciliation must not be confused with active resumable operations. It also rejects
  `operation.submittedAt` values that precede
  `operation.createdAt` and `operation.expiresAt` values that do not follow `operation.submittedAt`,
  preserving the async operation lifecycle ordering and positive bounded-wait window.
- Durable external-operation reconciliation shared TCK cases now reject late callback
  `receivedAt` timestamps with invalid seconds in the Rust runner, aligning callback receipt
  timestamp validation with Python before accepting late usage reconciliation evidence.
- Durable external-operation reconciliation shared TCK cases now reject zero-year operation
  `createdAt` timestamps in the Rust runner, preserving RFC 3339-style year bounds before late
  usage or side-effect reconciliation evidence is accepted.
- Durable external-operation reconciliation shared TCK cases now reject operation `createdAt`,
  `submittedAt`, `expiresAt`, and late callback `receivedAt` timestamps that use a space instead
  of the RFC 3339 `T` separator in the Python runner, keeping operation lifecycle and late receipt
  timestamp validation aligned with the Rust runner before reconciliation evidence is accepted.
- Durable external-operation reconciliation shared TCK cases now reject compact timezone offsets
  such as `+0000` on `operation.createdAt`, `operation.submittedAt`, `operation.expiresAt`, and
  `lateCallback.receivedAt`, so Python cannot accept operation lifecycle or callback receipt
  evidence that the Rust TCK parser treats as non-RFC3339.
- Durable external-operation reconciliation shared TCK cases now expose `diagnosticCount` on
  successful projections and include a valid fractional-second late callback `receivedAt` case,
  keeping Rust and Python aligned on RFC 3339-style callback receipt timestamps before late usage
  is reconciled.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `operation.providerOperationId` evidence in both Rust and Python runners before accepting
  provider-backed late callback reconciliation.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `operation.releaseId` evidence in both Rust and Python runners before accepting
  release-compatible late callback reconciliation.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `operation.tenantId` evidence in both Rust and Python runners before accepting
  tenant-isolated late callback reconciliation.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `operation.policySnapshotId` evidence in both Rust and Python runners before accepting
  policy-governed late callback reconciliation.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `lateCallback.callbackId` evidence in both Rust and Python runners so late provider callbacks
  remain tied to an auditable callback receipt.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.operationId` to be present and match `operation.operationId` in both Rust and
  Python runners before a late callback can prove it belongs to the reconciled async operation.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.providerOperationId` to be present and match `operation.providerOperationId` in
  both Rust and Python runners before a late callback can prove it belongs to the provider job.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.runId` to be present and match `operation.runId` in both Rust and Python runners
  before a late callback can prove it belongs to the reconciled run.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.nodeId` to be present and match `operation.nodeId` in both Rust and Python runners
  before a late callback can prove it belongs to the reconciled graph node.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.attemptId` to be present and match `operation.attemptId` in both Rust and Python runners
  before a late callback can prove it belongs to the reconciled run attempt.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.releaseId` to be present and match `operation.releaseId` in both Rust and Python
  runners before a late callback can prove it belongs to the compatible release.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.tenantId` to be present and match `operation.tenantId` in both Rust and Python
  runners before a late callback can prove it belongs to the reconciled tenant.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.payloadDigest` to be a canonical `sha256:` digest in both Rust and Python runners
  before late callback content can prove reconciliation behavior.
- Durable external-operation reconciliation shared TCK cases now require `lateCallback.status` to be
  one of the terminal async-operation result statuses in both Rust and Python runners before late
  callback evidence can prove reconciliation behavior.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `lateCallback.verifiedBy` evidence in both Rust and Python runners before an external callback
  can prove authenticated late-result reconciliation.
- Durable external-operation reconciliation shared TCK cases now reject `lateCallback.verifiedBy`
  values that are explicitly `unauthenticated`, so late-result reconciliation cannot be proven by
  a nonblank unauthenticated placeholder.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `lateCallback.idempotencyKey` evidence in both Rust and Python runners before a late callback can
  prove idempotent external-operation reconciliation.
- Durable external-operation reconciliation shared TCK cases now require nonblank
  `lateCallback.policySnapshotId` evidence in both Rust and Python runners before a late callback
  can prove policy-governed reconciliation.
- Durable external-operation reconciliation shared TCK cases now require
  `lateCallback.policySnapshotId` to match `operation.policySnapshotId` in both Rust and Python
  runners before a late callback can prove it was reconciled under the persisted policy snapshot.
- Python TCK validation now compares the late-callback operation, provider-operation, run, node,
  attempt, release, tenant, and policy snapshot identities as exact durable strings rather than
  trim-normalized values, so whitespace-mutated callback evidence cannot satisfy reconciliation
  conformance by matching only after normalization.
- Python TCK validation also rejects surrounding whitespace on the operation-side reconciliation
  evidence fields (`operationId`, `providerOperationId`, `idempotencyKey`, `runId`, `nodeId`,
  `attemptId`, `releaseId`, `tenantId`, `policySnapshotId`, and `expectedSchema`) before those
  fields can drive callback matching, resume fencing, or late usage reconciliation checks.
- Python TCK validation now applies the same exact-value rule to late-callback receipt evidence,
  rejecting surrounding whitespace on callback/run/node/attempt/release/tenant identities,
  payload digest, terminal status, verifier identity, idempotency key, policy snapshot, and
  `receivedAt` before parsing timestamps or comparing callback evidence to the operation envelope.
- Durable external-operation reconciliation shared TCK cases now require ISO
  `lateCallback.receivedAt` evidence in both Rust and Python runners before a late callback can
  prove a durable callback receipt timestamp, and reject callback receipts recorded before
  `operation.submittedAt` or after `operation.expiresAt`.
- Durable external-operation reconciliation shared TCK cases now require nonempty object-shaped
  provider usage records with nonblank `metric` and non-negative integer `amount` fields in both
  Rust and Python runners when late usage is marked reconciled, so billing reconciliation cannot be
  proven from a bare flag or placeholder usage object.
- The application-protocol TCK runner now applies the protocol metadata default release id when
  fixtures omit `releaseId`, so malformed sequence and replay-limit cases validate the intended
  integer contracts instead of failing earlier on unrelated release metadata.
- The PyO3 bridge now mirrors the Rust runtime-core application-protocol metadata contract
  (`releaseId` and `operationId`) and serializes newer tool admission/resolution errors such as
  expired policy decisions and empty resolution-scope entries, keeping native Python bindings
  buildable against the normative Rust runtime.
- Rust and shared usage TCK fixtures now treat provider-response replay as an exact duplicate
  receipt: the provider response id, attempt, timestamp, amounts, and metadata must match before an
  existing usage record is replayed instead of rejected as a conflict.
- The Rust durable-runtime TCK runner now covers the shared async/background durable cases for
  accepted background run replay, callback delivery projection, callback resume guards, cancel/
  callback race ordering, and late external-operation reconciliation, matching the Python testing
  package's interpretation of those fixtures.
- The shared durable TCK now includes a 429 callback-delivery retry case and the Rust durable TCK
  runner reports `retryScheduledAfterRetryableStatus`, keeping Rust/Python conformance aligned for
  retryable webhook receiver responses beyond 5xx.
- Durable callback projection validation now has shared expected-diagnostic coverage for malformed
  duplicate 409 receiver rows that are not recorded as `acknowledged`, keeping duplicate delivery
  evidence distinct from failed or retryable deliveries.
- The shared durable TCK now includes a 410 subscription-gone callback delivery case. Python and
  Rust durable runners both report `subscriptionGoneAfter410` only when the receiver status is
  `410`, the delivery is `cancelled`, and the durable error is `subscription_gone`.
- Durable callback projection validation now has shared expected-diagnostic coverage for malformed
  410 receiver rows that are not recorded as cancelled `subscription_gone` deliveries, preventing
  subscription-gone evidence from being represented as a generic failed delivery.
- The shared durable TCK now includes a successful 2xx callback delivery case. Python and Rust
  durable runners both report `deliveredAfter2xx` only when the receiver status is 2xx and the
  durable delivery row is recorded as `delivered`.
- The shared durable TCK now also supports expected validation diagnostics and includes a malformed
  2xx callback delivery case, so Python and Rust both reject a successful receiver acknowledgement
  that is durably recorded as a failed callback delivery.
- The shared durable TCK now includes a non-retryable 4xx callback delivery case. Python and Rust
  durable runners both report `nonRetryable4xxTerminal` only for non-409/410/429 4xx receiver
  responses recorded as failed `non_retryable` deliveries.
- Durable callback projection validation now has shared expected-diagnostic coverage for malformed
  non-special 4xx receiver rows that are not recorded as failed `non_retryable` deliveries, keeping
  terminal non-retryable evidence distinct from cancellation, duplicate, gone, and retryable
  receiver outcomes.
- The shared durable TCK now also includes a missing-redrive callback projection case. Python and
  Rust runners both observe `deadLetterPreservesEventId` and `redriveCreatesApplicationEvent` as
  false when no explicit `redrive` envelope is present, while Python still rejects attempts to
  prove true redrive behavior without that evidence.
- Production conformance evidence now treats durable async/background execution as part of the MVP
  acceptance boundary: `GB-C4-PRODUCTION` requires durable TCK coverage and the
  `coding-agent-background-callbacks` acceptance application, so accepted invocation, cursor replay,
  callback journal-before-resume, and signed callback delivery cannot be omitted from production
  claims.
- The upstream conformance-profile catalog is now tested for byte-for-byte semantic parity with the
  shipped `src/graphblocks/data/conformance-profiles.yaml` profile set, preventing documentation
  copies from dropping inherited TCK suites or async/background production evidence.
- Conformance profile parsing now treats `extends`, `requires`, `tck`, and
  `acceptanceApplications` as explicit list-of-strings contracts, preventing malformed YAML
  mappings or scalar values from being silently coerced into claimed profile requirements.
- The upstream package catalog is now tested for semantic parity with the shipped
  `src/graphblocks/data/package-catalog.yaml` install catalog, so optional integration packages,
  foundation package ownership, and MVP metapackage dependencies cannot drift between docs and the
  packaged source of truth.
- The standard policy profile set now includes the amendment's `assistant-output-standard`
  bounded-holdback output streaming profile, with generation, client-delivery, and output-commit
  enforcement points plus abort-response violation handling for pending tool calls and delivered
  drafts.
- The upstream bundle `SHA256SUMS` manifest is now covered by an integrity test that hashes every
  file in `docs/upstream/GraphBlocks_v1.0_Final` except the manifest itself, so documentation,
  catalog, profile, and example edits must refresh the bundle checksum evidence before commit.

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
- The shared durable TCK now includes a callback/cancel race fixture where the cancellation journal
  entry wins before `ExternalCallbackReceived`; the callback may be recorded and late usage
  reconciled, but it cannot resume the run or commit a result under the same ownership fence.
- callback receipt is journaled before resume.
- coordinator failover resumes once.
- budget exhaustion during wait records callback but pauses resume.
- policy/release compatibility is re-evaluated on resume.
- mandatory callback failure pauses or dead-letters according to policy.
- large callback payload is rejected or converted to `ArtifactRef`.
- dead-letter redrive does not create duplicate `ApplicationEvent`.
- non-mandatory webhook outage does not block run completion.
- external operation side-effect commit is preserved after cancellation.

Implementation note: the durable shared TCK now includes async callback contract fixtures for
background detach/cursor replay, webhook retry/duplicate/dead-letter projection, callback
auth/schema/stale-attempt/budget resume guards, unauthenticated callback rejection,
non-`ExternalCallbackReceived` receipt-promotion rejection, and late external-operation
reconciliation. These
fixtures keep the central rule executable: runs outlive clients, the event stream and journals are
authoritative, and callbacks are projections or authenticated resume signals rather than sources of
truth.

## 5. Phase 2 — AI Core Profiles (`GB-C2-AI-APPLICATION`)

### Documents

- Artifact/SourceAsset/Revision/ParsedDocument/Element/Chunk
- Python document lineage primitives now reject whitespace-wrapped artifact ids, URIs, source asset
  ids, revision ids, document ids, element kinds, span/source/chunk references, metadata keys, and
  element-id collections instead of trimming them before parser selection, chunking, or citation
  lineage can depend on those identifiers. The shared documents TCK now keeps artifact inputs exact
  while preserving parser descriptor normalization coverage.
- Python parser descriptors and deterministic selection locks now reject whitespace-wrapped
  processor ids, versions, lock reasons, artifact checksum evidence, and metadata keys. MIME type
  and extension entries remain normalized only for parser matching.
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
- Ingestion processor refs, index record refs, manifest identities, ACL/error fields, chunk ids,
  and metadata keys now reject values that only become valid after trimming, preserving exact
  document-ingestion provenance across parser, chunker, embedding, and publish records.
- RAG primitives cover local chunk indexing, tombstone/hard delete propagation, context packs,
  citation/source-trace resolution with retrieval rank/score/metadata provenance, answer grounding,
  abstention, fusion, and rerank projections.
- RAG freshness filtering now validates `minimum_source_modified_at` and source-modified metadata as
  strict RFC 3339-style datetimes, rejecting permissive space-separated or timezone-less forms before
  context packing and freshness metrics can treat stale evidence as current.
- RAG request, result, context, and citation primitives now reject whitespace-wrapped retrieval ids,
  retriever ids, knowledge item refs, citation/claim ids, metadata keys, and provenance lists before
  context packing, citation tracing, fusion, or reranking can depend on normalized identifiers.
- Conversation primitives cover CAS, tombstone/hard delete retention, branch/regenerate lineage,
  turn lifecycle, draft/retract semantics, and deterministic conflict handling.
- `ContentPart` JSON data and metadata now recursively validate strict JSON values at construction
  time, rejecting arbitrary Python objects, tuples, and non-finite numbers before conversation
  state, tool output, callback output, or policy-reviewed content can persist them.

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
- Rust usage ledgers now reject directly appended reconciliation records whose `reconciliation_of`
  source record is missing, so callers cannot bypass the `reconcile(...)` source-existence guard.
- Rust usage ledgers now require every `Reconciled` usage record to identify its
  `reconciliation_of` source and reject `reconciliation_of` on non-reconciled records, preserving
  clear late-final-usage lineage for direct appends and generated reconciliations.
- Rust usage ledgers now reject conflicting provider-response replays for the same
  `(provider_response_id, attempt_id)`, including replays that drift the recorded
  `occurred_at_unix_ms`; only exact logical replays with different local record ids remain
  idempotent.
- Rust usage ledgers now reject records with an empty `amounts` list, so late usage reconciliation
  and billing/quota projections cannot persist a meaningless no-op usage record as authoritative.
- SQLite usage ledger replay now re-runs the normal Rust `UsageRecord` validation after parsing
  stored JSON, so malformed durable rows with negative amounts or blank amount fields cannot enter
  late usage totals after restart.
- Python SQLite usage ledger replay now parses stored `amounts_json` and `metadata_json` with
  strict JSON semantics, rejecting non-standard constants such as `NaN` before late provider usage
  can re-enter reconciliation or totals after restart.
- Python and Rust usage reconciliation now reject `occurred_at` timestamps that precede the source
  usage record, preserving the amendment's late-final-usage ordering for in-memory and SQLite
  ledgers in both implementations.
- The Python usage ledger now validates usage and reconciliation `occurred_at` fields as strict
  RFC 3339-style datetimes, rejecting whitespace-normalized, timezone-less, lowercase-`z`, and
  compact-offset forms before ledger append, replay, or late reconciliation ordering can observe
  them.
- Python `UsageAmount` now rejects blank dimension values, matching the Rust runtime's usage record
  validation for non-empty dimension keys and values before budget or usage ledger admission.
- The budget-race TCK runner now validates expected reserved and available `UsageAmount` values
  through the same schema path as runtime budget inputs, so boolean amounts cannot be silently
  treated as integer `1` in concurrency conformance fixtures.
- `ExhaustionController` now rejects externally supplied continuation permits that lack concrete
  reservation refs, fencing tokens, or permit identity fields, so budget resume admission cannot
  bypass the ledger-issued authority shape required for fenced background execution.
- The exhaustion TCK runner now passes continuation permit admission epochs through `BudgetPermit`
  validation instead of coercing them, so boolean fencing/admission values cannot authorize resumed
  budget work.
- `ExhaustionController` now validates its own admission epoch and every admission `work_epoch`
  before comparing continuation boundaries, preventing boolean values from being treated as epoch
  `1` during budget pause/resume decisions.
- Inline exhaustion admission permits in the TCK runner now use the same raw `BudgetPermit`
  admission-epoch validation as stored continuation permits, so per-admission top-up permits cannot
  hide boolean fencing values.
- Python `BudgetPermit` validation now requires positive fencing token values, aligning the
  authoring/schema facade with the Rust runtime admission guard for fenced budget continuation.
- Python `BudgetPermit` expiry now validates as a strict RFC 3339-style datetime at construction
  and permit-time checks, so malformed or timezone-less continuation permits cannot authorize
  budgeted commit/release paths.
- Python SQLite budget ledger snapshots now save and replay `state_json` with strict JSON
  semantics, rejecting non-standard constants such as `NaN` before corrupted budget state can
  authorize pause/resume or permit accounting after restart.
- SQLite budget permit replay now revalidates reservation refs, usage amount projections, and
  positive fencing tokens before a stored permit can authorize resume, commit, release, or expire
  paths after restart.
- SQLite completion reserve replay now revalidates usage amount projections, spender authority,
  positive fencing tokens, and held-budget ids before a stored reserve can authorize finalization
  work after restart.
- `ExhaustionController` now models `checkpoint_and_pause` as safe suspension work: checkpoint and
  cleanup can proceed after exhaustion without requiring a top-up continuation permit, while new
  provider work, finalization, optional tasks, and trials remain denied. Explicit continuation
  step/usage bounds still apply, and the behavior is covered by the shared exhaustion TCK.
- `degrade_then_finalize` now admits only best-effort finalization and cleanup after exhaustion
  without requiring a top-up permit. Optional tasks, state-changing effects, unreserved provider
  calls, and other new work remain denied; explicit continuation bounds still apply and are covered
  by the shared exhaustion TCK.
- Python `ExhaustionController` now mirrors the Rust admission behavior for `checkpoint_and_pause`
  and `degrade_then_finalize`, allowing only the safe checkpoint/cleanup or best-effort
  finalization paths without a continuation top-up permit.

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
- Python OTLP and Langfuse projection contracts now parse stored exporter payloads with strict JSON
  semantics and require JSON object roots, rejecting non-standard constants such as `NaN` before
  telemetry payloads are handed to lossy observability exporters.

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
- Python lease pools now deep-freeze and JSON-validate lease attributes, rejecting nested mutable
  aliases, non-string mapping keys, non-JSON values, and non-finite floats before ownership/fencing
  metadata can be persisted on an active lease.
- Python `LeasePool` now validates acquisition, expiration, and reap timestamps as strict RFC
  3339-style datetimes, so malformed lease times cannot silently admit or reap scarce-resource
  grants.
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
- Python workspace snapshots, commits, mutation decisions, and mutation policies now reject
  whitespace-wrapped workspace/snapshot/commit/change-set ids, metadata keys, reason codes, and
  policy selector values before CAS commits or protected-resource checks can depend on normalized
  identities.
- Python review requests and reviewer credentials now deep-freeze nested metadata while thawing
  those snapshots for canonical review-request digests. Request ids, credential refs, review scopes,
  and nested metadata keys are exact values, so retained caller references or whitespace-normalized
  review identities cannot alter review evidence or digest inputs after construction.
- The Python review facade now validates review request, reviewer credential, and recorded review
  timestamps as strict RFC 3339-style datetimes before credential expiry, active-review checks, or
  review workflow state can observe them.
- The Python evaluation facade now validates review and model-visible-tool timestamps as strict
  RFC 3339-style datetimes, rejecting space-separated forms, timezone-less values, lowercase `z`,
  compact offsets such as `+0000`, and surrounding whitespace before review invalidation or
  model-tool visibility windows can be projected.
- Python evaluation review records now reject whitespace-wrapped review ids, subject digests,
  scopes, and credential refs before review evidence can satisfy workspace trial gates or result
  bundle provenance.
- Python model-visible tool provenance refs now reject whitespace-wrapped tool names, resolved-tool
  ids, definition/binding digests, and policy snapshot ids before run provenance can record the tool
  set exposed to a model invocation.
- Python evaluation resource snapshots, evidence refs, and typed-value refs now reject
  whitespace-wrapped resource ids, digests, schema ids, encodings, evidence kinds, URIs, and
  metadata keys before result bundles or trial checks can treat them as provenance evidence.
- Python evaluation check, metric, gate, and trial result primitives now reject
  whitespace-wrapped check ids, metric names and units, gate ids, violated constraint refs, trial
  ids, and usage refs before quality gates or result bundles can depend on normalized evidence.
- Python result bundles now reject whitespace-wrapped bundle, run, release, deployment revision,
  usage record, and policy decision refs before durable evaluation provenance is hashed or
  persisted.
- Python run provenance now rejects whitespace-wrapped graph, release, deployment revision,
  physical plan, and release-signature digests while validating start/completion timestamps before
  provenance snapshots are attached to result bundle hashes.
- Python evaluation change sets now reject whitespace-wrapped change-set ids, non-snapshot
  base/candidate refs, and invalid operation keys before workspace trials can freeze or hash
  candidate mutations.
- Python SLO objectives, measurements, and reports now reject whitespace-wrapped SLO ids,
  indicators, windows, units, and reason codes before operational evidence is evaluated or
  attached to result bundles.
- `graphblocks-runtime-core::tui::TuiRunView` now projects `GetRunStatus` and `AttachToRun`
  replay results into duplicate-tolerant terminal rows, preserving cursor-expired recovery metadata
  without making the TUI the source of truth for run state.
- The Python TUI workspace projection now ignores boolean `tool_result_sequence` metadata in
  `JobProgress` payloads, rendering an `updated` fallback instead of displaying booleans as stream
  sequence numbers.

## 10. Phase 7 — Optional Extensions

### Voice (`GB-X2-VOICE`)

DuplexSession, transport, VAD authority, interruption classifier, playback ledger, provider realtime adapter를 별도 package에서 구현한다.

### Current implementation slice

- `graphblocks-runtime-core::voice` now includes the core duplex session contract, transport
  metadata, VAD authority, interruption classifier, playback ledger, realtime session request, and
  a pure `RealtimeProviderAdapter` projection that binds provider id, endpoint, auth secret ref,
  defaults, options, and stable provider-session digests without adding a network client to
  runtime-core. Python voice and WebRTC contract objects reject boolean and non-integer
  timing/sequence/index fields before stream ordering, ICE candidate ordering, playback
  interruption, or provider realtime requests can persist them.

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
- The Python Kafka, Pub/Sub, and SQS durable adapters now reject boolean and non-integer partition,
  offset, sequence, delivery-attempt, and timestamp cursor fields before projecting records into
  durable `SourceCursor` and `SourceEvent` contracts.
- Python durable checkpoint barriers now apply the same integer contract to `state_revision`,
  `created_at_unix_ms`, and `schema_versions`, preventing boolean protocol values from being
  coerced into checkpoint revisions or schema version records.
- Python durable tool-terminal and response-policy-stop records now reject boolean and non-integer
  revisions, stream sequence numbers, and completion/cutoff timestamps before durable replay or
  terminal-state enforcement can observe them.
- `InMemoryDurableSource.poll` now validates demand before replay slicing, so malformed demand
  values return durable demand errors instead of raw host-language slice exceptions.
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
