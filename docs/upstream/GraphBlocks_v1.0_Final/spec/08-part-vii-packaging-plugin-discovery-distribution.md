# Part VII. Packaging, Plugin Discovery, Distribution

## 193. Packaging goals

GraphBlocks packaging은 다음 목표를 만족해야 한다.

1. `pip install graphblocks`가 모든 provider, cloud SDK, parser, DB client를 설치하지 않는다.
2. Graph authoring/validation은 native runtime 없이도 가능하다.
3. 실행 runtime은 provider integration과 독립적으로 업그레이드할 수 있다.
4. 하나의 integration 설치/삭제가 core package 파일을 덮어쓰거나 제거하지 않는다.
5. plugin 탐색은 heavy SDK import 없이 가능하다.
6. missing dependency 오류가 필요한 distribution 이름과 설치 명령을 알려 준다.
7. Python과 standalone Rust deployment가 같은 GraphSpec/plan을 실행한다.
8. official/community integration을 독립 release할 수 있다.
9. package compatibility가 lockfile과 TCK로 검증 가능하다.
10. `graphblocks-all` 같은 비대한 공식 bundle을 제공하지 않는다.

## 194. 배포물 계층

```text
Layer 0: Schema and authoring
Layer 1: Native runtime
Layer 2: Provider-neutral domain packs
Layer 3: Tooling and surfaces
Layer 4: Provider/framework integrations
Layer 5: Optional runtime extensions
```


### Package 분리 기준

패키지는 namespace 수가 아니라 dependency와 운영 경계로 나눈다. 다음 중 하나 이상이면 별도 distribution을 SHOULD 사용한다.

- 무거운 provider/cloud/DB/parser dependency를 추가한다.
- native wheel 또는 system binary가 필요하다.
- core와 다른 release cadence 또는 보안 대응 주기를 가진다.
- runtime process 격리 또는 별도 credential boundary가 필요하다.
- 선택적 product profile 또는 transport를 제공한다.
- 독립 maintainer/support tier가 필요하다.

다음 이유만으로는 패키지를 분리하지 않는다.

- block namespace가 다르다.
- 동일 SDK로 여러 SPI를 구현한다.
- 파일 수가 많다.
- 문서상 chapter가 다르다.

예를 들어 하나의 `graphblocks-postgres` integration은 RecordStore, StateStore, ConversationStore, CoordinationBackend를 함께 제공할 수 있다. `graphblocks-pgvector`는 vector-specific dependency와 capability가 독립적일 때만 별도로 둔다.

### Dependency 방향 원칙

```text
core ← domain contracts ← provider integrations
  ↑          ↑
  └─ tooling/runtime/extension은 필요한 방향으로만 의존
```

- domain package는 `graphblocks-core`에 의존하고 `graphblocks-runtime`에는 의존하지 않는다.
- `graphblocks-policy`는 core schema에만 의존하며 external PDP adapter를 기본 dependency로 포함하지 않는다.
- `graphblocks-budget`은 `graphblocks-usage`와 분리하며, distributed ledger backend는 integration package로 제공한다.
- provider package는 core와 필요한 domain contract에만 의존한다.
- server/worker/runtime package가 provider integration을 역으로 dependency에 포함하지 않는다.
- application package가 최종 provider 조합과 version range를 소유한다.
- dependency cycle은 build 및 release gate에서 실패한다.

## 195. Base distributions

### `graphblocks-core`

**역할:** 가장 작은 순수 Python authoring/validation package.

제공:

- import package `graphblocks`
- canonical AI types
- GraphSpec/ApplicationSpec/BindingSpec/Release/Deployment schema
- BlockDescriptor SDK
- compiler frontend와 static validation
- plugin manifest reader
- generated type stubs

금지 dependency:

- PyO3 native runtime
- web server/UI framework
- provider/cloud/DB SDK
- Langfuse SDK
- PDF/OCR parser
- Kubernetes/Terraform SDK

### `graphblocks-runtime`

**역할:** Native Rust runtime Python binding.

제공:

- `graphblocks_runtime`
- native extension `graphblocks_runtime._native`
- scheduler, cancellation, bounded sequence, flow control
- Python block adapter와 worker protocol client

특정 provider, DB/cloud connector, parser, web server, voice/media package에 의존하지 않는다.

### `graphblocks-stdlib`

Provider/domain에 독립적인 lightweight block만 포함한다.

```text
value.*
schema.*
control.*
sequence.*
text.*
json.*
prompt.const/file/compose/render
memory/local test connector
```

다음은 stdlib에 넣지 않는다.

```text
document.*
query/retrieve/rank/context/answer.*
conversation.*
agent/tool.*
provider/cloud/db/parser integration
```

### `graphblocks` standard metapackage

`pip install graphblocks`는 GraphBlocks의 주력인 문서/RAG/대화 graph를 provider-neutral하게 작성하고 local 실행할 수 있어야 한다.

```text
graphblocks-core
graphblocks-runtime
graphblocks-stdlib
graphblocks-documents
graphblocks-rag
graphblocks-conversation
graphblocks-policy
graphblocks-budget
graphblocks-usage
graphblocks-cli
```

이 package들은 pure Python 또는 GraphBlocks native runtime wheel만 포함하고, 특정 LLM SDK, vector DB client, cloud SDK, PDF/OCR engine, server framework를 기본 dependency로 가져오지 않는다. `graphblocks-budget`와 `graphblocks-usage`의 기본 설치는 in-memory/SQLite 개발 구현과 SPI만 제공하며 production distributed backend는 별도 integration으로 설치한다.

가장 작은 설치는 metapackage가 아니라 필요한 distribution을 직접 선택한다.

```bash
pip install graphblocks-core
pip install graphblocks-core graphblocks-runtime graphblocks-stdlib
```

## 196. Domain feature distributions

| Distribution | 기능 | 기본 metapackage |
|---|---|---|
| `graphblocks-documents` | document profile, lineage, manifest, orchestration | 포함 |
| `graphblocks-rag` | Retriever, fusion/rerank, context, answer/citation | 포함 |
| `graphblocks-conversation` | conversation/turn transaction, compaction | 포함 |
| `graphblocks-agents` | tool loop, approval, agent state | 선택 |
| `graphblocks-evaluation` | generic check/metric/gate/trial/result bundle | 선택 |
| `graphblocks-policy` | policy composition, typed obligation, default evaluator | 포함 |
| `graphblocks-orchestration` | TaskPlan/TaskPlanPatch, model/worker pool | 선택 |
| `graphblocks-review` | review workflow와 credential verifier SPI | 선택 |

Domain package는 provider SDK나 parser engine을 포함하지 않는다. Canonical foundational schema는 core가 소유하고 profile-specific block/config는 domain package가 소유한다.

## 197. Application and tooling distributions

| Distribution | 책임 |
|---|---|
| `graphblocks-cli` | validate, plan, run, lock, doctor, release/deploy 명령 |
| `graphblocks-server` | HTTP/SSE/WebSocket, auth hooks, health endpoints |
| `graphblocks-client` | local/remote client와 app command/event protocol |
| `graphblocks-tui` | Textual 기반 reference TUI; client에만 의존 |
| `graphblocks-workspace` | snapshot/fork/ChangeSet/check/review/CAS commit과 file/git/test/process tool |
| `graphblocks-worker` | isolated Python worker process/pool |
| `graphblocks-testing` | deterministic runtime, test DSL, TCK clients |
| `graphblocks-devtools` | graph visualization, migration, profiling, codegen |

`graphblocks-tui`가 parser, vector DB, provider SDK, native runtime을 직접 의존해서는 안 된다.

## 198. Deployment and operations distributions

| Distribution | 책임 |
|---|---|
| `graphblocks-deployment` | GraphRelease, GraphDeployment, DeploymentRevision, physical planner |
| `graphblocks-oci` | release bundle push/pull, digest, signature/provenance helpers |
| `graphblocks-kubernetes` | Kubernetes/Helm renderer, cluster capability inspection |
| `graphblocks-terraform` | infrastructure requirement와 module input/output bridge |
| `graphblocks-gitops` | Argo CD/Flux-compatible release manifest adapter |
| `graphblocks-operator` | 별도 controller image/Helm chart; standard pip install에 미포함 |
| `graphblocks-telemetry` | canonical observation/capture/redaction policy |
| `graphblocks-otel` | OTLP exporter와 Collector templates |
| `graphblocks-prometheus` | metric exporter, dashboards/rules |
| `graphblocks-langfuse` | telemetry/prompt/eval/dataset adapters |
| `graphblocks-audit` | durable audit sink SPI/implementations |
| `graphblocks-usage` | durable actual usage ledger, provider reconciliation, immutable usage facts |
| `graphblocks-budget` | budget/quota allocation, atomic reservation/settlement, entitlement adapter |
| `graphblocks-policy-opa` | OPA/Rego policy decision adapter |
| `graphblocks-policy-cedar` | Cedar authorization decision adapter |
| `graphblocks-dashboards` | generated dashboards, alerts, runbooks |

Kubernetes, Terraform, Langfuse, Prometheus, OPA, Cedar SDK는 base runtime dependency가 아니다.

## 199. Provider integration distributions

Naming convention:

```text
graphblocks-<technology>
```

Import package는 충돌을 피하기 위해 고유 top-level 이름을 사용한다.

```text
Distribution: graphblocks-openai
Import:       graphblocks_openai

Distribution: graphblocks-qdrant
Import:       graphblocks_qdrant
```

**중요:** integration distribution은 `graphblocks/` 디렉터리에 파일을 추가하지 않는다. `graphblocks-core`만 public `graphblocks` import package를 소유한다.

### Model providers

```text
graphblocks-openai
graphblocks-anthropic
graphblocks-google-genai
graphblocks-azure-openai
graphblocks-bedrock
graphblocks-huggingface
graphblocks-ollama
graphblocks-vllm
```

### Document converters

```text
graphblocks-pypdf
graphblocks-docling
graphblocks-markitdown
graphblocks-tika
graphblocks-unstructured
graphblocks-hwp
```

### Knowledge and storage

```text
graphblocks-qdrant
graphblocks-pgvector
graphblocks-opensearch
graphblocks-elasticsearch
graphblocks-pinecone
graphblocks-weaviate
graphblocks-milvus

graphblocks-s3
graphblocks-gcs
graphblocks-azure-blob

graphblocks-firestore
graphblocks-mongodb
graphblocks-postgres
graphblocks-redis
```

### Observability and framework

```text
graphblocks-langfuse
graphblocks-haystack
graphblocks-langgraph
graphblocks-langchain
graphblocks-llamaindex
graphblocks-mcp
```

## 200. Extension distributions

```text
graphblocks-voice
graphblocks-webrtc
graphblocks-websocket-media
graphblocks-openai-realtime
graphblocks-silero-vad

graphblocks-durable
graphblocks-kafka
graphblocks-nats
graphblocks-sqs
graphblocks-pubsub
```

Voice나 durable stream package는 default `graphblocks` dependency가 아니다.

## 201. Dependency graph

```text
Application package
  ├─ graphblocks (meta)
  │    ├─ graphblocks-core
  │    ├─ graphblocks-runtime
  │    └─ graphblocks-stdlib
  ├─ selected domain packages ───────────────→ graphblocks-core
  ├─ selected provider integrations ─────────→ core + required domain contract
  ├─ selected tooling ───────────────────────→ core; runtime only when needed
  └─ selected extensions ────────────────────→ core/runtime/domain as declared
```

규칙:

- provider integration은 `graphblocks-core`와 필요한 domain contract에만 의존한다.
- integration이 `graphblocks` metapackage에 의존해서 불필요한 runtime/stdlib을 끌어오지 않도록 한다.
- runtime은 integration package에 의존하지 않는다.
- circular dependency를 금지한다.
- framework bridge는 해당 외부 framework와 core에 의존하되 다른 bridge에 의존하지 않는다.

## 202. 설치 프로파일

### Authoring/validation only

```bash
pip install graphblocks-core
```

용도:

- CI schema validation
- editor/IDE
- graph migration
- package manifest inspection

### Provider-neutral local runtime

```bash
pip install graphblocks
```

### Document ingestion

```bash
pip install \
  graphblocks \
  graphblocks-documents \
  graphblocks-pypdf \
  graphblocks-s3 \
  graphblocks-qdrant \
  graphblocks-openai
```

### RAG chatbot server

```bash
pip install \
  graphblocks \
  graphblocks-rag \
  graphblocks-conversation \
  graphblocks-server \
  graphblocks-openai \
  graphblocks-qdrant \
  graphblocks-postgres \
  graphblocks-langfuse
```

### Haystack interoperability

```bash
pip install graphblocks graphblocks-haystack
```

### Voice extension

```bash
pip install \
  graphblocks \
  graphblocks-conversation \
  graphblocks-voice \
  graphblocks-webrtc \
  graphblocks-openai-realtime
```

### Application dependency groups

Application repository는 development/test/documentation 도구에 standardized dependency groups를 사용할 수 있다.

```toml
[dependency-groups]
test = ["graphblocks-testing~=1.0", "pytest>=8"]
dev = ["graphblocks-cli~=1.0", "graphblocks-devtools~=1.0"]
docs = ["mkdocs-material"]
```

Dependency group은 배포 runtime dependency를 대신하지 않는다. Production image에는 application의 main dependencies와 선택한 runtime/provider package만 설치한다.

### Profile template은 distribution이 아니다

`rag-chat`, `document-ingestion`, `voice` 같은 profile은 project template 또는 generated dependency set으로 제공한다. 이를 `graphblocks-all`, `graphblocks-rag-all` 같은 장기 유지 bundle distribution으로 만들지 않는다.

```bash
graphblocks init --profile rag-chat
# pyproject.toml, graphblocks.lock template, sample connections 생성
```

## 203. Extras policy

Python extras는 소수의 convenience feature에만 사용한다.

```toml
[project.optional-dependencies]
cli = ["graphblocks-cli~=1.0"]
server = ["graphblocks-server~=1.0"]
testing = ["graphblocks-testing~=1.0"]
dev = ["graphblocks-cli~=1.0", "graphblocks-testing~=1.0", "graphblocks-devtools~=1.0"]
```

다음은 extras로 제공하지 않는다.

- 모든 model provider 목록
- 모든 DB/cloud connector
- 모든 parser
- voice와 durable stack 전체
- `all`

이유는 dependency resolution, security surface, wheel 크기, provider version 충돌을 통제하기 위해서다.

## 204. Namespace policy

공식 정책:

- `graphblocks-core`만 `graphblocks` import namespace를 소유한다.
- 다른 distribution은 `graphblocks_<integration>` 이름을 사용한다.
- PEP 420 namespace package로 여러 wheel이 같은 `graphblocks/` tree를 나눠 갖는 방식을 공식 기본으로 사용하지 않는다.
- 사용자는 integration module을 직접 import할 필요 없이 plugin registry를 통해 사용할 수 있다.

이 정책은 wheel uninstall 시 shared files가 손상되는 문제와 package ownership 불명확성을 줄인다.

## 205. Plugin discovery

Python package metadata entry point를 사용한다.

```toml
[project.entry-points."graphblocks.plugins"]
openai = "graphblocks_openai.plugin:load_plugin"
```

세부 group을 선택적으로 둘 수 있다.

```text
graphblocks.plugins
graphblocks.blocks
graphblocks.connectors
graphblocks.telemetry
graphblocks.prompt_registries
graphblocks.evaluators
graphblocks.framework_bridges
```

Registry는 heavy plugin module을 eager import하지 않는다.

## 206. Static plugin manifest

각 integration wheel은 static manifest를 포함해야 한다.

```json
{
  "manifest_version": 1,
  "plugin_id": "io.graphblocks.openai",
  "distribution": "graphblocks-openai",
  "plugin_version": "1.0.0",
  "maturity": "official",
  "requires_core": ">=1.0,<2.0",
  "requires_runtime_protocol": ">=1,<2",
  "plugin_api": ">=1,<2",
  "provides": [
    "model.provider:openai",
    "embedding.provider:openai"
  ],
  "blocks": [
    "model.chat@1",
    "embedding.text@1"
  ],
  "connections": ["model", "embedding"],
  "entry_point": "graphblocks_openai.plugin:load_plugin",
  "licenses": ["Apache-2.0"],
}
```

Manifest는 wheel의 dist-info에 `graphblocks-plugin.json` 이름으로 포함한다. Entry point metadata는 manifest locator와 lazy factory를 가리킨다. CLI가 manifest를 읽는 것만으로 provider SDK를 import해서는 안 된다.

Registry cache는 설치 distribution의 name/version, manifest hash, environment fingerprint로 무효화한다. Cache가 없거나 손상되어도 manifest 재탐색만 수행하고 integration SDK를 eager import하지 않는다.

## 207. Lazy loading

```text
scan installed distributions
→ read static manifests
→ build registry index
→ resolve graph requirements
→ import only selected plugin factory
→ instantiate only selected connection/block
```

Import 규칙:

- import 시 network connection을 열지 않는다.
- import 시 credential을 resolve하지 않는다.
- import 시 global event loop/task를 생성하지 않는다.
- optional SDK 누락 오류는 plugin load 단계에서 명확히 발생한다.

## 208. Plugin descriptor

```python
class PluginDescriptor(BaseModel):
    plugin_id: str
    version: str
    blocks: list[BlockDescriptor]
    connector_factories: list[ConnectorFactoryDescriptor]
    adapters: list[TypeAdapterDescriptor]
    capabilities: set[str]
    maturity: str
```

Plugin factory는 descriptor와 lazy factory를 반환한다.

## 209. Block registration conflict

동일 semantic block은 여러 implementation을 가질 수 있다.

```text
block: model.chat@1
implementations:
- openai
- anthropic
- google_genai
- local_openai_compatible
```

Conflict resolution:

1. GraphSpec `implementation`
2. connection provider
3. application binding
4. 유일한 implementation일 때만 자동 선택

동일 plugin ID/version 충돌이나 동일 implementation ID 중복은 startup error다.

## 210. Plugin trust policy

```yaml
plugins:
  allow:
    - io.graphblocks.*
    - com.company.*
  deny:
    - io.unknown.experimental
  maturity:
    minimum: official
  signatures:
    required: false
```

Production에서는 allowlist를 권장한다. 미신뢰 Python/native plugin은 in-process로 실행하지 않고 worker/remote 격리를 사용한다.

## 211. Package manifest validation

Official integration은 다음을 가져야 한다.

- static plugin manifest
- pyproject metadata
- README와 minimal usage example
- supported core/runtime range
- block/connector TCK 결과
- unit/integration tests
- security contact
- changelog
- license
- dependency upper/lower bound policy
- deprecation metadata, 해당 시

## 212. Compatibility dimensions

독립 version:

```text
GraphSpec API version
canonical schema version
block type version
runtime protocol version
plugin API version
Python distribution version
Rust crate version
provider adapter version
```

모든 것을 하나의 package SemVer로 암묵적으로 추론하지 않는다.

## 213. Foundation release train

다음 package만 coordinated minor release train을 따른다.

```text
graphblocks-core
graphblocks-runtime
graphblocks-stdlib
graphblocks-documents
graphblocks-rag
graphblocks-conversation
graphblocks-policy
graphblocks-budget
graphblocks-usage
graphblocks-testing
```

규칙:

- foundation package의 major.minor는 동일하게 유지한다.
- patch는 독립 배포할 수 있다.
- `graphblocks` metapackage는 검증된 foundation patch set과 선택한 CLI version을 pin한다.
- core/runtime protocol mismatch는 import 또는 runtime initialization에서 즉시 실패한다.

다음 first-party extension은 독립 SemVer를 사용하고 `requires_core`, `requires_runtime_protocol`, `plugin_api`, `schema_api` 범위로 호환성을 선언한다.

```text
graphblocks-agents
graphblocks-evaluation
graphblocks-orchestration
graphblocks-review
graphblocks-workspace
graphblocks-client
graphblocks-tui
graphblocks-cli
graphblocks-server
graphblocks-worker
graphblocks-deployment
graphblocks-telemetry
graphblocks-devtools
```

이 분리는 wheel을 작게 만드는 것뿐 아니라 optional feature 하나 때문에 foundation 전체를 다시 배포하는 일을 방지한다.

## 214. Integration release policy

Provider integration은 독립 SemVer를 사용한다.

예:

```toml
[project]
name = "graphblocks-qdrant"
version = "0.4.2"
dependencies = [
  "graphblocks-core>=1.0,<2.0",
  "graphblocks-rag>=1.0,<2.0",
  "qdrant-client>=1,<2"
]
```

Integration package version이 core version과 같을 필요는 없다.

## 215. Runtime protocol check

Python binding initialization:

```text
core expected runtime protocol
vs
native extension provided protocol
```

Mismatch error 예:

```text
RuntimeProtocolMismatch:
  graphblocks-core 1.0.2 requires protocol 1.x
  graphblocks-runtime 2.0.0 provides protocol 2.x
  install a compatible runtime: pip install "graphblocks-runtime>=1.0,<2.0"
```

## 216. Graph lockfile

```bash
graphblocks lock graph.yaml --output graphblocks.lock
```

Lockfile 내용:

```yaml
lockVersion: 1
graph:
  id: company-assistant
  graphHash: sha256:...
  schemaVersion: graphblocks.ai/v1alpha3

runtime:
  protocol: 1
  distribution: graphblocks-runtime
  version: 1.0.0

packages:
  - name: graphblocks-core
    version: 1.0.0
    hash: sha256:...
  - name: graphblocks-openai
    version: 0.3.1
    hash: sha256:...

plugins:
  - id: io.graphblocks.openai
    version: 0.3.1
    descriptorHash: sha256:...

blocks:
  model.chat@1:
    implementation: openai
    descriptorHash: sha256:...

prompts:
  - ref: company/rag-answer@12
    contentHash: sha256:...
```

Lockfile은 secret, access token, raw prompt variable을 포함하지 않는다.


### Environment lock과의 구분

`graphblocks.lock`은 Python dependency resolver의 environment lock을 대체하지 않는다.

| Lock | 책임 |
|---|---|
| `pylock.toml`, `uv.lock`, 또는 동등한 environment lock | Python wheel/sdist와 transitive dependency pin |
| `Cargo.lock` | standalone Rust build dependency pin |
| `graphblocks.lock` | graph/plan, block descriptor, plugin, prompt, schema, runtime protocol의 의미적 pin |
| container digest/SBOM | 배포 image와 system package pin |

Production reproducibility는 위 계층을 함께 사용한다. `graphblocks lock verify`는 environment에 설치된 distribution이 semantic lock과 일치하는지 검사하지만 package resolver 역할을 수행하지 않는다.

## 217. Lock modes

```text
strict
- exact package/plugin/descriptor hashes required

compatible
- declared version range 내 resolve 허용

unlocked
- development only
```

Production deploy는 strict 또는 approved compatible mode를 사용해야 한다.

## 218. Python wheel strategy

### Core

`graphblocks-core`는 pure Python universal wheel이다.

### Runtime

`graphblocks-runtime`은 Maturin/PyO3로 platform wheel을 배포한다.

지원 target 예:

```text
manylinux x86_64/aarch64
musllinux x86_64/aarch64
macOS x86_64/arm64
Windows x86_64/arm64, supported when toolchain permits
```

### Unsupported platform behavior

Native wheel을 제공하지 않는 platform에서도 `graphblocks-core`는 설치 및 validation이 가능해야 한다. 실행 시에는 다음 중 하나를 명시적으로 선택한다.

```text
build graphblocks-runtime from source
use RemoteRuntime/graphblocksd
use InProcessTestRuntime for tests only
```

Native extension import 실패를 silent pure-Python production runtime으로 자동 fallback하지 않는다.

### Stable ABI

CPython `abi3` 사용은 required PyO3 API와 성능 요구를 만족할 때 선택한다. 초기에는 Python minor별 wheel을 허용한다. 내부 runtime crate가 PyO3에 의존하지 않기 때문에 binding 전략을 바꿔도 core를 재설계할 필요가 없어야 한다.

## 219. Mixed Rust/Python project layout

```text
packages/graphblocks-runtime/
  Cargo.toml
  pyproject.toml
  python/
    graphblocks_runtime/
      __init__.py
      _typing.pyi
  src/
    lib.rs
```

Native module은 private 이름을 사용한다.

```toml
[tool.maturin]
python-source = "python"
module-name = "graphblocks_runtime._native"
```

Public Python API는 `graphblocks_runtime` wrapper를 통해 제공한다.

## 220. Rust crate packaging

Cargo workspace는 공통 lockfile과 build output을 공유한다. Publishable crate와 internal crate를 구분한다.

```toml
[workspace]
resolver = "3"
members = ["crates/*"]
default-members = [
  "crates/graphblocks-schema",
  "crates/graphblocks-runtime-core",
  "crates/graphblocks-python"
]
```

Internal crate에는 `publish = false`를 사용한다. Public Rust embedding API가 안정화되기 전에는 최소 crate만 crates.io에 공개한다.

## 221. Cargo feature policy

Cargo feature는 다음에 사용할 수 있다.

- platform allocator
- TLS backend
- optional telemetry exporter
- debug diagnostics
- compile-time performance option

다음에는 사용하지 않는다.

- 모든 model provider catalog
- 모든 document parser
- 모든 database connector
- user-facing plugin registry

Provider integration을 feature로 묶으면 Cargo feature unification 때문에 실제 dependency closure와 binary size가 불투명해질 수 있다.

## 222. Repository strategy

### Core monorepo

```text
graphblocks/
  crates/
  packages/
    graphblocks-core/
    graphblocks-runtime/
    graphblocks-stdlib/
    graphblocks-documents/
    graphblocks-rag/
    graphblocks-conversation/
    graphblocks-agents/
    graphblocks-evaluation/
    graphblocks-cli/
    graphblocks-server/
    graphblocks-worker/
    graphblocks-testing/
    graphblocks-devtools/
  specs/
  tck/
  examples/
```

### Official integrations monorepo

```text
graphblocks-integrations/
  integrations/
    openai/
    qdrant/
    s3/
    firestore/
    langfuse/
    haystack/
    ...
```

각 integration 디렉터리는 독립 `pyproject.toml`, tests, README, changelog를 가진다.

### Community integrations

외부 repository에서 독립 배포할 수 있다. Official registry 등록 전에 manifest validation과 TCK를 통과해야 한다.

## 223. Package naming rules

- PyPI distribution: lowercase kebab case, `graphblocks-<name>`
- Python import: lowercase snake case, `graphblocks_<name>`
- plugin ID: reverse DNS 또는 globally unique namespace
- semantic block ID: provider-neutral dotted name
- connection provider ID: short stable identifier

예:

```text
PyPI: graphblocks-google-genai
Import: graphblocks_google_genai
Plugin: io.graphblocks.google_genai
Provider: google_genai
```

## 224. Dependency policy

### Core direct dependency budget

`graphblocks-core`는 최소 dependency를 유지한다. 새로운 direct dependency는 다음을 검토한다.

- import time
- wheel size
- transitive dependency count
- license
- security history
- Python support range
- optionality

### No import-time side effects

모든 package는 import 시 다음을 금지한다.

- network call
- credential read
- background thread/task
- filesystem scan beyond package metadata
- telemetry exporter start
- logging global configuration overwrite

### Optional system dependencies

Tika server, LibreOffice, OCR engine, ffmpeg 같은 system dependency는 integration README와 capability doctor에서 명시한다. Core install 과정에서 자동 설치하지 않는다.

## 225. Dependency error ergonomics

```python
try:
    import qdrant_client
except ImportError as exc:
    raise MissingOptionalDependency(
        distribution="graphblocks-qdrant",
        dependency="qdrant-client",
        install="pip install graphblocks-qdrant",
    ) from exc
```

Generic `ModuleNotFoundError`를 그대로 사용자에게 노출하지 않는다.

## 226. Package size and startup targets

Normative requirement는 dependency boundary이며, 다음은 release target이다.

- `graphblocks-core` compressed wheel은 작고 pure Python이어야 한다.
- `graphblocks-runtime` wheel은 provider SDK와 parser asset을 포함하지 않는다.
- plugin registry scan은 integration SDK import 없이 완료되어야 한다.
- `import graphblocks`는 network/connector 초기화를 하지 않는다.
- 사용하지 않는 integration은 process memory에 load되지 않아야 한다.

Release CI는 wheel size와 import time regression을 기록한다.

## 227. CLI package commands

```bash
graphblocks packages list
graphblocks plugins list
graphblocks plugins inspect io.graphblocks.openai
graphblocks plugins validate dist/*.whl
graphblocks doctor graph.yaml
graphblocks lock graph.yaml
graphblocks lock verify graphblocks.lock
graphblocks env export --format requirements
graphblocks env sbom --format cyclonedx
```

## 228. Missing package diagnosis

`graphblocks doctor`는 다음을 검사한다.

- GraphSpec schema
- required plugin 설치
- core/runtime protocol
- connection capability
- system binary/service requirement
- credentials reference 존재 여부, 값은 출력하지 않음
- model/provider configuration
- package conflict
- deprecated integration

## 229. Integration TCK gate

Official integration release 전 필수:

```text
manifest TCK
block descriptor TCK
canonical serialization TCK
error mapping TCK
cancellation/timeout TCK
telemetry propagation TCK
connector-specific TCK
secret redaction TCK
```

Provider live tests는 nightly/credentialed job으로 분리하고 PR 기본 테스트는 deterministic mock을 사용한다.

## 230. Test extras

Optional dependency test는 설치되지 않은 환경에서 skip 또는 marker로 분리한다.

```text
unit
integration_mock
integration_live
contract
tck
benchmark
```

Core test suite가 모든 provider SDK 설치를 요구해서는 안 된다.

## 231. Release artifacts

각 release는 가능한 경우 다음을 생성한다.

- sdist
- wheel
- changelog
- SBOM(SPDX 또는 CycloneDX)
- checksums
- provenance/attestation
- TCK report
- supported platform matrix

Trusted publishing과 package signing은 release maturity에 따라 적용한다.

## 232. Deprecation

Plugin manifest:

```json
{
  "status": "deprecated",
  "deprecated_since": "0.9.0",
  "removal_after": "1.2.0",
  "replacement": "graphblocks-google-genai"
}
```

CLI와 compiler는 deprecated block/package 사용을 경고한다. Security issue가 있으면 normal deprecation window 없이 block할 수 있다.

## 233. Version pinning guidance

Application production lock:

```text
- core/runtime minor pin
- integration compatible range 또는 exact pin
- provider SDK transitive lock
- graphblocks.lock descriptor hash
- container/image digest
```

Library author는 지나치게 exact pin하지 않고 compatibility range를 선언한다.

## 234. Distribution support tier

| Tier | 소유자 | TCK | Release SLA | Registry 표시 |
|---|---|---|---|---|
| built-in | core team | mandatory | coordinated | built-in |
| official | core/integration team | mandatory | maintained | official |
| partner | named partner | mandatory | declared | partner |
| community | community | recommended | best effort | community |
| experimental | any | partial | none | experimental |

## 235. No mega package rule

공식 `graphblocks-all` distribution은 만들지 않는다.

이유:

- cloud SDK와 DB client 충돌
- 매우 큰 wheel/environment
- 보안 취약점 surface 증가
- platform-specific parser 설치 실패
- 사용하지 않는 native dependency load
- release cadence 결합

문서와 examples는 목적별 explicit install set을 제공한다. 조직 내부에서 curated constraints/bundle을 만들 수 있지만 core release artifact와 분리한다.

## 236. Application package 예

```toml
[project]
name = "company-knowledge-assistant"
version = "1.0.0"
dependencies = [
  "graphblocks>=1.0,<2.0",
  "graphblocks-rag>=1.0,<2.0",
  "graphblocks-conversation>=1.0,<2.0",
  "graphblocks-server>=1.0,<2.0",
  "graphblocks-openai>=0.3,<0.4",
  "graphblocks-qdrant>=0.4,<0.5",
  "graphblocks-postgres>=0.2,<0.3",
  "graphblocks-langfuse>=0.3,<0.4",
]

[dependency-groups]
test = [
  "graphblocks-testing>=1.0,<2.0",
  "pytest>=8",
]
docs = ["mkdocs-material"]
```

Application package가 실제 provider 조합을 소유한다.

## 237. Container image strategy

공식 image는 최소 계층으로 나눈다.

```text
graphblocks/runtime:<version>
- graphblocksd only

graphblocks/python-runtime:<version>
- Python + core/runtime/stdlib

graphblocks/dev:<version>
- CLI/testing/devtools
```

Provider별 모든 integration을 넣은 universal image를 기본 제공하지 않는다. Application image가 필요한 integration만 설치한다.

## 238. Standalone Rust distribution

```text
graphblocksd
- run compiled plans
- load remote/Python worker plugins
- expose HTTP/gRPC control plane
- no embedded provider SDK by default
```

Native Rust provider plugin은 정적 링크 또는 versioned process protocol을 우선한다. Rust dynamic library ABI를 public stable plugin contract로 간주하지 않는다.

## 239. Remote plugin protocol

언어/프로세스 격리가 필요한 integration은 remote protocol을 구현한다.

```text
DescribePlugin
DescribeBlock
InitializeConnection
Invoke
InvokeIncremental
Cancel
Health
Close
```

Protocol은 schema ID/version과 runtime protocol을 handshake한다.

## 240. Packaging acceptance criteria

1. `pip install graphblocks-core`는 provider SDK와 native wheel 없이 성공한다.
2. `pip install graphblocks`는 model/cloud/DB/parser SDK를 설치하지 않는다.
3. `graphblocks plugins list`는 provider SDK를 import하지 않는다.
4. integration uninstall이 `graphblocks` import package 파일을 삭제하지 않는다.
5. core/runtime protocol mismatch가 startup 전에 감지된다.
6. missing integration 오류에 distribution과 install command가 포함된다.
7. provider package는 독립적으로 release하고 TCK를 실행할 수 있다.
8. lockfile로 descriptor/package hash를 검증할 수 있다.
9. application은 필요한 provider만 explicit dependency로 선언할 수 있다.
10. voice/durable packages가 기본 설치에 포함되지 않는다.
11. wheel/platform matrix가 자동 CI에서 검증된다.
12. 모든 official wheel에 manifest와 license가 포함된다.
13. `graphblocks-stdlib`은 domain/provider package를 암묵적으로 설치하지 않는다.
14. environment lock과 `graphblocks.lock`의 불일치를 배포 전 검출한다.
15. unsupported native platform에서도 core validation과 RemoteRuntime 안내가 동작한다.

