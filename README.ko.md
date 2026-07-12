# GraphBlocks

[English](README.md) | [한국어](README.ko.md) | [简体中文](README.zh-CN.md)

> 바퀴를 다시 발명하지 마세요.

GraphBlocks는 이식 가능하고 테스트할 수 있으며 거버넌스를 적용할 수 있는 AI
애플리케이션을 위한 공급자 중립적 계약 도구 모음입니다. 특정 모델 공급자,
데이터베이스, 파서, 서버 프레임워크 또는 배포 플랫폼을 요구하지 않으면서 타입이
지정된 그래프, 런타임 동작, 애플리케이션 프로토콜, 정책 및 예산 경계, 패키지
메타데이터와 적합성 프로필을 정의합니다.

이 프로젝트는 알파 소프트웨어입니다. 호환성은 패키지나 디렉터리의 존재 여부가
아니라 적합성 프로필과 실행 가능한 증거에 근거해서만 주장됩니다.

## 포함된 구성 요소

- 작성, 검증, 기본 제공 블록, 참조 런타임, CLI 및 프레임워크 중립적 서버 계약을
  포함하는 순수 Python `graphblocks` SDK
- 선택 사항인 네이티브 `graphblocks-runtime` Python 확장
- `graphblocks-testing` 배포판 및 공유 TCK 픽스처
- Rust 스키마, 컴파일러, 프로토콜 및 런타임 크레이트
- 버전이 지정된 스키마 및 공급자 중립적 패키지 카탈로그
- 공유 TCK 픽스처 및 실행 가능한 인수 테스트 애플리케이션

## 개발 빠른 시작

Python 3.11 이상과 `rust-toolchain.toml`에서 선택한 Rust 툴체인이 필요합니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m graphblocks validate examples/01-enterprise-federated-rag/example.yaml
python examples/01-enterprise-federated-rag/run.py
python -m pytest
cargo test --workspace --all-targets
```

가상 환경을 활성화한 후 루트에서 편집 가능 모드로 설치하면 `graphblocks` 임포트
패키지와 `graphblocks` 명령, `python -m graphblocks`를 사용할 수 있습니다. 기본
제공 블록 구현과 CLI 및 서버 계약은 이 배포판에 포함되며, 별도의 기능별 wheel이
아닙니다. Extra는 실제 설치 의존성을 추가합니다. `runtime`은 네이티브 바인딩을,
`pdf`는 `pypdf`를, `test`는 pytest를 추가합니다. `graphblocks-tck` 명령을
사용하려면 `graphblocks-testing`을 설치하세요.

기계 판독형 패키지 카탈로그는 릴리스 아티팩트와 이식 가능한 구성 요소 및 바인딩
식별자를 구분합니다. 구성 요소 항목은 별도로 배포되는 Python wheel에 대응하지
않습니다. Python 릴리스 범위는 `graphblocks`, `graphblocks-runtime`,
`graphblocks-testing`으로 이루어집니다.

이 저장소에서는 `validate`, `plan`, `run`을 위한 Python 비의존 Rust 실행 파일인
`graphblocks-native`도 빌드합니다. 이 실행 파일은 표준 입력에서 JSON 또는 YAML을
받고, 여러 문서로 이루어진 YAML 스트림에서 이름이 지정된 `Graph`를 선택할 수
있으며, 네이티브 stdlib 블록 세트를 실행합니다. `graphblocksd`는 워커 제어 평면
명령이며, 아직 요청을 수신하는 HTTP/서버 프로세스는 아닙니다.

## 문서

- [문서 안내](docs/README.md)
- [설치](docs/getting-started/installation.md)
- [빠른 시작](docs/getting-started/quickstart.md)
- [아키텍처](docs/concepts/architecture.md)
- [지속적으로 갱신되는 명세](docs/specification/README.md)
- [적합성](docs/development/conformance.md)
- [구현 상태](docs/project/status.md)
- [예제](examples/README.md)

## 프로젝트 및 커뮤니티

GraphBlocks는 [Apache License 2.0](LICENSE)에 따라 라이선스가 부여됩니다.
기여를 환영합니다. [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md),
[GOVERNANCE.md](GOVERNANCE.md)를 참고하세요.
