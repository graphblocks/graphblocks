# GraphBlocks v1.0 Final Architecture Bundle

이 번들은 GraphBlocks의 최종 아키텍처 기준선, 구현 계획, package catalog, policy/conformance profile, acceptance application 의사 코드를 포함한다.

## 핵심 파일

- `spec/00-complete.md`: 통합 최종 스펙
- `FINAL_REVIEW.md`: 최종 아키텍처 검토와 잔여 위험
- `IMPLEMENTATION_PLAN.md`: 단계별 구현 계획과 종료 기준
- `USE_CASE_VALIDATION.md`: 사례별 일반화 검증
- `catalog/package-catalog.yaml`: package 경계와 구현 phase
- `catalog/conformance-profiles.yaml`: 적합성 claim 기준
- `profiles/policy-profiles.yaml`: 표준 quota exhaustion profile
- `examples/`: RAG, ingestion, chatbot, TUI, authority research, orchestrator, RTL trial, K8s, observability, voice
- `VALIDATION_REPORT.md`: 정적 검증 결과
- `SHA256SUMS`: bundle integrity manifest

## 확정된 범위

Foundation은 natural language, files, documents, RAG, conversation, policy/budget/usage와 Rust runtime이다. Orchestration/workspace는 선택 extension이며 voice와 durable unbounded stream은 experimental extension이다.

## 설치 모델

```text
pip install graphblocks
```

기본 metapackage는 provider-neutral foundation만 설치한다. Provider, parser, DB/cloud SDK, server, TUI, Kubernetes/Terraform, voice는 별도 distribution으로 설치한다. 공식 `graphblocks-all` package는 만들지 않는다.

## 구현 시작점

`IMPLEMENTATION_PLAN.md`의 Phase 0과 Phase 1을 먼저 진행하고, governed chatbot vertical slice를 첫 end-to-end acceptance target으로 사용한다.
