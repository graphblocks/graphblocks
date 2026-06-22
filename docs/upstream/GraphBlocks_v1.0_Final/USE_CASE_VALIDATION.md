# Acceptance Use Cases and Architecture Validation

이 문서는 예시를 제품 기능 목록으로 취급하지 않는다. 각 사례는 core contract가 특정 산업에 종속되지 않고 재사용되는지를 검증하는 acceptance fixture다.

| 사례 | 주로 검증하는 계약 |
|---|---|
| Enterprise RAG | named binding, federated retrieval, context budget, citation, turn transaction |
| Document preprocessing | revision, map checkpoint, parser lock, OCR fallback, staged commit/delete |
| Governed chatbot | BudgetPermit, finish-current-turn, hard-stop, draft/commit/retract |
| TUI workspace assistant | Application Protocol, approval, artifact preview, reconnect |
| Authority-backed advisory | generic SourceRef/Evidence/Claim, live snapshot, substantive Review |
| Research orchestrator | bounded TaskPlan, evidence, independent checks, budget delegation |
| RTL verified trial | Snapshot/ChangeSet, sandbox, Check/Gate/Metric, LeasePool, oracle integrity |
| Kubernetes production | release pinning, execution group, placement, drain, canary and SLO |

## 일반화 판단

법률, 연구, RTL을 core package로 추가할 필요는 없다. 다음 공통 흐름으로 표현된다.

```text
versioned input/source
→ bounded plan or static graph
→ isolated work/mutation
→ evidence and diagnostics
→ deterministic/model checks
→ gate and substantive review
→ commit/publish
```

Domain-local schema와 blocks는 application 또는 integration package에서 제공하고, GraphBlocks foundation은 source/evidence/snapshot/change/check/gate/review/task/permit semantics만 제공한다.

## 예시가 발견한 필수 compiler rule

- branch의 미실행 값을 `null`로 취급하지 않는다.
- protected source와 test oracle을 writable workspace에 포함하지 않는다.
- remote boundary에서 native/Python 객체를 전달하지 않는다.
- TaskPlan이 max task/depth/budget/context-access를 선언하지 않으면 거부한다.
- state-changing effect는 idempotency와 exhaustion behavior가 없으면 거부한다.
- citation/review/gate는 subject/source digest와 release identity를 기록한다.
- release, prompt, index, policy, package/image reference가 mutable하면 production build를 거부한다.
