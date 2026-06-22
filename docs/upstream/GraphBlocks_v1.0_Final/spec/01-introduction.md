# GraphBlocks Specification

## Version 1.0 — Final Architecture and Implementation Baseline

- Project: GraphBlocks
- Tagline: **Typed blocks and a Rust-native runtime for document AI, RAG, conversations, agents, and production AI applications**
- Document status: Final architecture baseline and normative implementation contract
- API maturity: public object APIs remain alpha until their TCK profiles pass; document finalization does not imply API GA
- Document date: 2026-06-22
- Supersedes: Draft v0.3 through Draft v0.8
- Primary scope: natural language, files, document processing, retrieval, chat, agents, policy, usage governance, evaluation, deployment, and operations
- Optional extensions: realtime voice, durable unbounded dataflow
- Intended readers: runtime and SDK engineers, block and integration authors, application developers, platform engineers, SRE, security, and evaluation teams

---

## 문서 구성

이 명세는 하나의 거대한 설치 패키지나 하나의 구현 process를 전제하지 않는다. 규범적 계약은 Part로 분리되고, Python distribution, Rust crate, container image, worker pool도 같은 경계에 맞춰 나뉜다.

| 문서 Part | 핵심 내용 | 대표 구현 패키지 |
|---|---|---|
| Part I | 제품 범위와 canonical AI data model | `graphblocks-core` |
| Part II | Graph IR, block contract, Rust runtime | `graphblocks-core`, `graphblocks-runtime` |
| Part III | 파일과 문서 처리 | `graphblocks-documents` |
| Part IV | retrieval, RAG, context, citation | `graphblocks-rag` |
| Part V | conversation, memory, agent, tools | `graphblocks-conversation`, `graphblocks-agents` |
| Part VI | ApplicationSpec, client protocol, integrations, connectors | `graphblocks-client`, integration packages |
| Part VII | packaging, plugin discovery, distribution | 모든 distribution에 공통 |
| Part VIII | immutable release, placement, Kubernetes, Terraform, rollout | deployment packages |
| Part IX | execution records, OpenTelemetry, Langfuse, SLO, operations | observability packages |
| Part X | policy, quota, budget, entitlement, resource governance | `graphblocks-policy`, `graphblocks-budget`, `graphblocks-usage` |
| Part XI | security, testing, diagnostics, roadmap | `graphblocks-testing`, tooling packages |
| Extension A | realtime voice와 duplex session | `graphblocks-voice` 계열 |
| Extension B | durable unbounded dataflow | `graphblocks-durable` 계열 |

## 최종 확정 상태와 적합성 프로필

이 문서는 GraphBlocks의 **구현 기준선**을 확정한다. 이후 변경은 단순한 문구 수정이 아니라 object API, canonical schema, runtime protocol 또는 conformance profile의 version 변경으로 관리한다. 문서가 Version 1.0이라는 사실과 개별 API가 `v1alpha*`라는 사실은 모순되지 않는다. 전자는 아키텍처 기준선의 확정이고, 후자는 구현 및 TCK가 완료되기 전의 API 성숙도다.

### 안정성 등급

| 등급 | 범위 | 의미 |
|---|---|---|
| Normative Core | Part I, Part II, Part VII의 package/plugin contract, Part X의 policy/budget semantics, Part XI의 TCK | 호환 구현이 반드시 따라야 하는 계약 |
| Normative Profile | Documents, RAG, Conversation, Application Protocol, Release/Deployment/Observability object | 해당 profile을 구현한다고 주장할 때 필수 |
| Provisional Extension | TaskPlan orchestration, workspace trial/review, Kubernetes operator | 공개 contract는 유지하되 구현 피드백으로 minor revision 가능 |
| Experimental Extension | Realtime Voice, Durable Unbounded Dataflow | 기본 설치 및 Core conformance에 포함되지 않음 |

### 적합성 프로필

| ID | 구현이 제공해야 하는 범위 |
|---|---|
| `GB-C0-SCHEMA` | canonical schema, GraphSpec parse/normalize/hash, plugin manifest validation |
| `GB-C1-LOCAL-RUNTIME` | Rust scheduler, typed ports, `Outcome<T>`, cancellation, journal, local flow, Python binding |
| `GB-C2-AI-APPLICATION` | Document/RAG/Conversation profile과 provider-neutral acceptance applications |
| `GB-C3-GOVERNED-RUNTIME` | Policy PEP, UsageLedger, BudgetLedger, permit, exhaustion boundary, approval/review/gate |
| `GB-C4-PRODUCTION` | immutable release, worker protocol, placement, drain, deployment revision, audit/SLO/telemetry |
| `GB-X1-ORCHESTRATION` | bounded TaskPlan/TaskPlanPatch, worker/model pool, task budget delegation |
| `GB-X2-VOICE` | duplex session, VAD authority, interruption, playback ledger |
| `GB-X3-DURABLE-STREAM` | unbounded source offset, watermark, checkpoint and sink commit semantics |

구현과 package는 지원하는 profile만 주장해야 한다. 예를 들어 local Python SDK가 `GB-C1`을 통과했다고 해서 Kubernetes deployment나 durable stream 적합성을 주장해서는 안 된다.

### 아키텍처 동결 규칙

- 도메인별 업무 객체는 core package에 추가하지 않고 `SourceRef`, `EvidenceRef`, `ResourceSnapshotRef`, `ChangeSet`, `Check/Gate/Review`, `TaskPlan` 조합으로 표현한다.
- 모델이 normalized Graph IR 또는 production GraphSpec을 직접 수정하지 않는다. 동적 작업은 bounded `TaskPlan`으로 제한한다.
- Kubernetes/Terraform 세부 필드는 GraphSpec에 들어가지 않는다. Graph는 요구 조건을, Deployment는 placement를, 플랫폼 adapter는 실제 resource를 정의한다.
- telemetry backend, Langfuse 또는 Prometheus를 correctness, quota, billing, audit의 source of truth로 사용하지 않는다.
- block마다 별도 Pod를 만들지 않는다. remote boundary는 ExecutionGroup과 target 단위로 설계한다.
- 하나의 거대한 Python wheel 또는 container image를 공식 배포 단위로 만들지 않는다.

## 1. 규범 키워드

이 문서의 `MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, `MAY`는 각각 필수, 금지, 권고, 비권고, 선택 요구사항을 뜻한다.

## 2. 공개 객체와 API version

Version 1.0의 규범 객체는 다음과 같다.

```text
GraphSpec               graphblocks.ai/v1alpha3
CompositeBlockSpec      graphblocks.ai/v1alpha3
ApplicationSpec         graphblocks.ai/v1alpha1
BindingSpec             graphblocks.ai/v1alpha1
GraphRelease            graphblocks.ai/v1alpha1
GraphDeployment         graphblocks.ai/v1alpha1
ObservabilityProfile    graphblocks.ai/v1alpha1
EvaluationSpec          graphblocks.ai/v1alpha1
PolicyBundle            graphblocks.ai/v1alpha1
PolicyProfile           graphblocks.ai/v1alpha1
PolicySnapshot          graphblocks.policy/PolicySnapshot@1
```

`v1alpha1` GraphSpec의 단일 `connection`과 generic `control.parallel/control.join`은 migration reader에서 허용할 수 있지만, compiler는 이를 v1alpha3 normalized IR의 named resource binding과 구체적 control primitive로 변환해야 한다. `v1alpha2`의 document-only `SourceSpan`, chat-centric `DatasetCase`, untyped dynamic plan은 migration adapter가 범용 source/evidence, typed case, TaskPlan contract로 변환한다.

## 3. 공개 호환성 단위

GraphBlocks의 공개 호환성은 다음 단위로 관리한다.

1. object API version과 normalized IR format version
2. canonical schema ID와 schema version
3. BlockDescriptor, typed port, resource slot contract
4. runtime 및 worker protocol version
5. plugin API와 static manifest version
6. connector/provider SPI version
7. release bundle와 physical plan format version
8. package compatibility range와 package lock
9. checkpoint, conversation, manifest store schema version
10. telemetry mapping profile version

Rust 내부 타입 레이아웃, Tokio task 구조, PyO3 함수 배치, Kubernetes renderer의 내부 구현은 공개 ABI가 아니다.

## 4. 객체 계층

```text
Authoring DSL/YAML
        ↓
GraphSpec                     논리적 계산과 상태 전이
PolicyBundle / PolicyProfile  권한, quota, budget, lifecycle obligation
ApplicationSpec               사용자 표면, route, command/event protocol
BindingSpec                   model/store/retriever/prompt/secret reference
        ↓
Normalized Graph IR           모든 port, adapter, policy가 명시된 언어 중립 IR
        ↓
GraphRelease                  graph, app, package, prompt, policy bundle의 불변 릴리스
GraphDeployment               환경별 desired state와 placement/rollout
        ↓
DeploymentRevision            binding과 target을 해석한 불변 revision
PhysicalExecutionPlan         node/group/target/transport/implementation 계획
        ↓
Rust runtime, worker pools, Kubernetes workloads, external services
```

Terraform은 GraphBlocks runtime 객체가 아니다. Terraform은 cluster, node pool, network, storage, IAM과 GraphBlocks Helm/operator 배포를 관리하며, GraphBlocks는 infrastructure requirement와 module input/output bridge를 제공한다.

## 5. 핵심 설계 원칙

1. **자연어와 파일이 코어다.** 음성 및 범용 stream은 일반 `Message`, `Document`, `ToolCall`, `Answer` 모델을 확장한다.
2. **Rust가 실행을 소유한다.** Python은 첫 authoring SDK와 provider/custom block 계층이다.
3. **Graph IR은 언어 중립적이다.** Python 임의 객체나 provider SDK 객체는 공개 port와 remote wire contract에 들어가지 않는다.
4. **Graph, application, binding, deployment를 분리한다.** 계산, 사용자 표면, 외부 자원, 물리적 위치를 한 YAML에 혼합하지 않는다.
5. **의미와 구현을 분리한다.** `model.chat`, `document.convert`, `retrieve.hybrid`는 의미 block이며 provider는 binding/implementation으로 선택한다.
6. **출처와 증거를 잃지 않는다.** source asset에서 chunk, retrieval, claim, citation, check 결과까지 lineage와 `SourceRef`/`EvidenceRef`를 보존한다.
7. **검색의 공개 추상화는 Retriever다.** vector database는 여러 구현 수단 중 하나다.
8. **상태 변경은 명시적이다.** 외부 write, tool, delete, publish는 effect, idempotency, approval, audit 계약을 가진다.
9. **부재는 null이 아니다.** branch skip, cancellation, failure, 값 `null`을 `Outcome<T>`로 구분한다.
10. **독립 node는 자동 병렬 실행한다.** 명시적 task group은 취소, deadline, quorum, partial failure 정책이 있을 때만 사용한다.
11. **incremental output과 commit을 구분한다.** UI draft delta는 durable final message가 아니며 commit/retract 의미론을 가진다.
12. **release는 불변이다.** graph, prompt, index revision, package lock, image digest, policy를 pin한 release 없이 production run을 시작하지 않는다.
13. **관측성과 correctness 기록을 분리한다.** execution journal, audit, usage ledger는 durable하며 OTel telemetry는 진단 plane이다.
14. **OpenTelemetry가 vendor-neutral base다.** Langfuse는 LLM observability, prompt, evaluation, dataset integration이다.
15. **기본 설치는 제품 중심이되 가볍다.** 문서/RAG/conversation 계약은 포함하지만 provider SDK, parser, DB/cloud client, server, voice는 선택 설치다.
16. **plugin은 지연 로드한다.** installed distribution 탐색만으로 provider SDK를 import하지 않는다.
17. **명세는 TCK와 acceptance application으로 검증한다.** 예시 YAML만으로 의미론을 정의하지 않는다.
18. **정책은 prompt가 아니라 runtime 계약이다.** 권한, quota, budget, tool/effect, data capture, review/gate는 typed decision과 enforcement point를 가진다.
19. **사용량과 예산을 분리한다.** UsageLedger는 실제 사용량, BudgetLedger는 allocation/reservation/settlement를 소유한다.
20. **초과 시 종료 경계를 명시한다.** 현재 turn/task/item을 완료할지, checkpoint할지, 즉시 취소할지 policy가 atomic unit과 overdraft를 정의한다.
21. **도메인 객체를 core에 축적하지 않는다.** Snapshot, ChangeSet, Evidence, Check, Gate, Review, TaskPlan 같은 공통 work contract로 일반화한다.

## 6. 핵심 문장

> **GraphBlocks는 자연어와 파일이 graph를 통과하는 동안 타입, 출처, 권한, 예산, 실행 의미론, 검증, release identity, 관찰 가능성을 보존하는 Rust-native AI application runtime이다.**

## 7. 비목표

초기 코어는 다음을 목표로 하지 않는다.

- 범용 Apache Beam/Flink 대체
- 모든 provider 기능의 완전한 최소공통분모화
- Python 임의 객체의 자동 직렬화
- 미신뢰 native plugin의 in-process 실행
- block마다 Kubernetes Pod 하나를 만드는 실행 모델
- Terraform state나 Kubernetes CRD를 run/event store로 사용하는 것
- 하나의 `graphblocks-all` 패키지/이미지에 parser, provider, DB, media stack을 모두 포함하는 것
- LangGraph, Haystack, LangChain 내부 scheduler의 완전한 복제

## 8. 권장 읽기 순서

일반 애플리케이션 개발자는 Part I, III, IV, V, VI, VII을 먼저 읽는다. Runtime/SDK 구현자는 Part II를 추가로 읽는다. Production 운영자는 Part VIII, IX, X, XI를 읽는다. Voice 또는 범용 unbounded stream이 필요할 때만 Extension A/B를 적용한다.

## 9. v0.8 대비 최종 확정 변경

- GraphSpec API가 `v1alpha3`로 상승한다.
- document-only `SourceSpan`은 `SourceRef + SourceLocator`로 일반화되고 기존 값은 `DocumentSpan` variant로 migration한다.
- `SearchHit.document`는 `KnowledgeItemRef`로 변경한다.
- `Claim`, `Diagnostic`, `EvidenceRef`, `ResourceSnapshotRef`, `ChangeSet`, `ReviewRecord`, `Check/Gate/Trial`, `ResultBundle`을 canonical contract로 정의한다.
- `DatasetCase`는 chat 전용 field 집합에서 typed input/expected/assertion 구조로 변경한다.
- `Approval`과 substantive `Review`를 분리한다.
- model이 graph topology를 직접 수정하지 않고 optional `TaskPlan/TaskPlanPatch` executor를 사용한다.
- `Outcome<T>`에 `Denied`, `BudgetExhausted`, `Paused`를 추가한다.
- UsageLedger의 quota 책임을 분리해 `BudgetLedger`가 allocation/reservation/settlement를 소유한다.
- PolicyBundle/PolicyProfile, typed obligation, exhaustion boundary를 공개 계약으로 추가한다.
- `graphblocks-policy`, `graphblocks-budget`, `graphblocks-orchestration` package 경계를 추가한다.

