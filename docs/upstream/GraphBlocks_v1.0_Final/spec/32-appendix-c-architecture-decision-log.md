# Appendix C. Architecture Decision Log

## C.1 Product core

문서, 자연어, RAG, conversation을 core로 유지하고 voice와 범용 stream은 extension으로 둔다.

## C.2 Runtime ownership

Rust runtime이 scheduler, cancellation, bounded flow, leases, terminal state를 소유한다. Python은 authoring/provider/custom block 계층이다.

## C.3 Layered specs

GraphSpec, ApplicationSpec, BindingSpec, GraphRelease, GraphDeployment를 분리한다.

## C.4 Control semantics

Automatic DAG concurrency를 기본으로 하고 generic parallel/join을 구체적 primitive로 해체한다.

## C.5 Outcome semantics

Absent, skipped, failed, cancelled, null을 명시적으로 구분한다.

## C.6 Packaging

Standard metapackage에는 provider-neutral documents/RAG/conversation을 포함하되 provider/parser/cloud/server/voice는 분리한다.

## C.7 Release and operations

Production run은 immutable release와 deployment revision에 pin하고 workload-aware rollout/drain을 적용한다.

## C.8 Observability

ExecutionJournal, AuditLog, UsageLedger, BudgetLedger, ApplicationEventStream, Telemetry를 분리한다.

## C.9 Policy enforcement

Policy는 prompt/observer가 아니라 compile, admission, node, provider, effect, commit, publish enforcement point를 가진다.

## C.10 Usage exhaustion

Finish-current-unit과 hard-stop을 모두 지원하되 unit, overdraft, draft 처리, effect atomicity를 반드시 명시한다.

## C.11 Cross-domain work contracts

법률, 연구, Verilog 같은 domain package를 core에 추가하지 않고 Snapshot, ChangeSet, Evidence, Check/Gate/Trial, Review, ResultBundle, TaskPlan으로 일반화한다.

