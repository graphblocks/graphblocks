# GraphBlocks Specification v1.0 Change Log

## v0.8 → v1.0

- 문서 상태를 Final Architecture and Implementation Baseline으로 확정했다.
- API maturity와 문서 version을 분리했다.
- Normative Core, Normative Profile, Provisional/Experimental Extension 안정성 등급을 추가했다.
- `GB-C0`~`GB-C4`, `GB-X1`~`GB-X3` conformance profile을 추가했다.
- coordinated release train을 foundation package로 축소했다.
- optional first-party package와 provider integration을 독립 SemVer로 분리했다.
- 구현 roadmap을 version milestone이 아닌 dependency phase로 재구성했다.
- OpenTelemetry GenAI mapping을 별도 versioned adapter로 명확히 했다.
- 법률, 연구, RTL 사례를 domain package가 아닌 generic acceptance fixture로 정리했다.
- implementation plan, final review, use-case validation, parseable example set을 번들에 추가했다.

## 호환성

GraphSpec object API는 계속 `graphblocks.ai/v1alpha3`이다. v0.8 normalized graph와 canonical schema에 의도적인 추가 breaking change는 없다. v1.0은 문서/구현 기준선의 확정이며, API GA 선언이 아니다.
