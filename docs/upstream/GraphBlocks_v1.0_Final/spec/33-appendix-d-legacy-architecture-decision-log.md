# Appendix D. Legacy Architecture Decision Log

## D.1 Native backend 유지

Draft v0.3의 핵심 결정인 NativeBackend 우선 원칙을 유지하되 이름을 `NativeRustRuntime`으로 명확히 했다.

## D.2 Framework backend 축소

`LangGraphBackend`를 전체 GraphBlocks 의미론을 구현하는 동급 backend로 보지 않는다. 대신 turn-level subgraph bridge로 정의한다. Haystack도 component/pipeline bridge로 통합한다.

## D.3 EjectedBackend 재분류

Ejection은 실행 backend가 아니라 배포/code-generation target이다.

## D.4 FlowBlock 정리

Semaphore, rate limit, retry, timeout은 기본적으로 node wrapper/scheduler policy다. Wait 결과가 graph data일 때만 explicit flow node를 사용한다.

## D.5 Streaming 재분류

Draft v0.4의 event streaming과 data streaming 분리는 유지한다. 다만 LLM token delta는 finite invocation의 incremental projection으로 이동하고, raw media와 unbounded dataflow는 extension으로 이동한다.

## D.6 Voice 재배치

VAD, duplex session, interruption, playback ledger 설계는 유지하되 core conversation model 위의 Extension A로 이동한다.

## D.7 DocumentStore 명칭 변경

일반 구조화 저장소는 `RecordStore`, 검색용 저장소는 `KnowledgeIndex`, 검색 공개 계약은 `Retriever`로 분리한다.

## D.8 Event-sourcing 범위 축소

모든 request를 event-sourced로 강제하지 않는다. Durable job과 audit effect에 필요한 경우만 EventStore/checkpoint를 요구한다.

## D.9 Package architecture 강화

Core/runtime/domain/integration/extension을 별도 distribution으로 배포한다. 공식 mega package는 제공하지 않는다.

## D.10 Policy와 quota 분리

Draft v0.3의 flow rate limit과 Draft v0.7의 UsageLedger만으로는 entitlement와 in-flight exhaustion을 정의할 수 없다. v0.8은 PolicyBundle, BudgetLedger, reservation, exhaustion boundary를 추가한다.

