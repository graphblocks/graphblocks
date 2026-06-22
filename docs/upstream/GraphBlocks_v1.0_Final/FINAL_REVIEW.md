# GraphBlocks v1.0 Final Architecture Review

## 결정

GraphBlocks v1.0은 **구현 착수 가능한 아키텍처 기준선**으로 승인한다. 기존 설계를 다시 뒤집을 구조적 결함은 발견되지 않았다. 다만 구현 범위가 넓으므로 모든 기능을 한 번에 구현하는 것이 아니라 conformance profile과 acceptance application 단위로 진행해야 한다.

문서 버전 1.0은 아키텍처의 확정을 의미한다. `GraphSpec v1alpha3`, `GraphDeployment v1alpha1` 등의 API는 각 TCK가 통과되기 전까지 alpha 상태를 유지한다.

## 최종적으로 유지한 핵심 결정

1. 자연어, 파일, 문서, RAG, conversation이 제품 코어다.
2. Rust native runtime이 scheduler, cancellation, bounded flow, journal, policy enforcement hook을 소유한다.
3. Python은 authoring SDK, provider integration, custom block, isolated worker 계층이다.
4. Graph, Application, Binding, Release, Deployment를 분리한다.
5. GraphSpec은 정적이고, 동적 작업은 bounded TaskPlan으로 제한한다.
6. domain-specific type을 core에 추가하지 않고 source/evidence/snapshot/change/check/gate/review/task contract로 일반화한다.
7. 정책, 사용량, 예산은 prompt나 observer가 아니라 runtime plane이다.
8. UsageLedger, BudgetLedger, ExecutionJournal, AuditLog, Telemetry를 분리한다.
9. Kubernetes는 worker workload의 실제 node 배치를 담당하고, GraphBlocks는 ExecutionTarget과 placement를 결정한다.
10. 공식 배포는 작은 foundation package와 독립 first-party extension/integration package로 나눈다.

## 마지막 검토에서 수정한 사항

### Release train 축소

모든 first-party package의 minor version을 동시에 올리는 방식은 package 분리의 장점을 약화한다. 따라서 coordinated foundation train은 core/runtime/stdlib/documents/RAG/conversation/policy/budget/usage/testing으로 제한했다. Agent, orchestration, workspace, server, deployment, observability package는 독립 SemVer와 compatibility range를 사용한다.

### 적합성 프로필 추가

구현이 지원하지 않는 영역까지 “GraphBlocks compatible”이라고 주장하는 일을 막기 위해 `GB-C0`부터 `GB-C4`, 선택 extension `GB-X1`부터 `GB-X3`까지 적합성 프로필을 추가했다.

### 안정성 등급 명시

TaskPlan, Kubernetes operator, voice, durable stream은 core와 동일한 안정성으로 취급하지 않는다. Core contract와 optional extension의 성숙도를 분리했다.

### OpenTelemetry GenAI adapter versioning

GenAI semantic convention은 별도 저장소로 이동하고 발전 중이므로 GraphBlocks canonical observation을 특정 외부 attribute set에 직접 고정하지 않는다. versioned mapping adapter를 유지한다.

## 구현 전 반드시 검증할 기술 위험

| 위험 | 검증 방법 | 실패 시 대응 |
|---|---|---|
| Rust↔Python 경계 비용 | token/item batch, cancellation, callback benchmark | hot path를 Rust에 유지하고 Python worker 경계를 굵게 조정 |
| scheduler/journal 결합 복잡도 | deterministic runtime TCK, fault injection | journal write boundary와 state reducer를 단순화 |
| distributed effect 중복 | idempotency/fencing chaos test | exactly-once 표현을 금지하고 effect journal/compensation 강화 |
| provider usage 지연·부정확성 | provisional usage reconciliation test | permit safety margin과 late settlement 정책 강화 |
| package/plugin 확산 | package closure test와 static manifest | first-party extension 승인 기준과 deprecation policy 강화 |
| remote payload 과대 전송 | physical-plan payload-size diagnostics | ArtifactRef transfer를 강제 |
| metric cardinality·민감정보 | telemetry lint, capture policy TCK | high-cardinality label 및 full-content capture 차단 |
| deployment/operator 과도한 범위 | renderer 우선 구현 | operator는 renderer와 revision contract 안정화 후 진행 |

## 비차단 잔여 결정

다음은 architecture blocker가 아니라 구현 선택 사항이다.

- worker RPC의 실제 transport와 binary encoding
- canonical schema code generation 도구
- EventStore/RunStore의 첫 production backend
- Kubernetes renderer가 생성하는 chart 구조
- external PDP의 우선 지원 순서
- provider별 cancellation 및 usage reconciliation adapter 세부

이 항목은 공개 의미론을 바꾸지 않는 범위에서 구현 ADR로 결정한다.

## Go/No-Go 기준

다음 세 vertical slice가 통과되면 architecture가 실제 구현에서도 유효하다고 판단한다.

1. **Governed chatbot**: draft/commit/retract, token reservation, finish-current-turn, hard-stop, late usage reconciliation.
2. **Document RAG**: file revision, converter plugin, chunk lineage, ACL retrieval, citation validation, index publish/delete.
3. **Remote verified work**: snapshot, isolated ChangeSet, Check/Gate/Review, worker placement, cancellation, journal resume.

세 slice가 동일 core/runtime contract를 재사용해야 하며, domain-specific 예외가 runtime에 직접 추가되면 안 된다.
