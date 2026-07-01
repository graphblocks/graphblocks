# Part VIII. Immutable Release, Placement, Deployment, and Infrastructure

## 241. 운영 plane

```text
Management Plane
- compile, lock, release, sign, GitOps, Terraform/Kubernetes reconciliation

Control Plane
- admission, scheduling, worker registry, leases, ownership, cancellation, checkpoint orchestration

Data Plane
- Rust runtime service, Python/Rust worker pools, provider/connectors, parser/OCR/sandbox

Observation Plane
- telemetry, audit, usage, evaluation, SLO, release analysis
```

초기 구현이 한 process여도 책임과 protocol은 분리해야 한다.

## 242. Release object hierarchy

```text
GraphSpec + ApplicationSpec + Binding template + package/environment locks
        ↓
GraphRelease / ReleaseBundle (immutable)
        ↓
GraphDeployment (desired state)
        ↓
DeploymentRevision (resolved immutable revision)
        ↓
PhysicalExecutionPlan
        ↓
RuntimeInstance / WorkerPool / Kubernetes workload
```

## 243. GraphRelease와 ReleaseBundle

`GraphRelease`는 production에 배포할 불변 artifact 집합이다. `.gbr` archive 또는 OCI artifact로 저장할 수 있다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: GraphRelease
metadata:
  name: enterprise-rag
  version: 2026.06.22.1

spec:
  bundle:
    digest: sha256:...
    mediaType: application/vnd.graphblocks.release.v1

  application:
    hash: sha256:...

  graphs:
    chat:
      graphHash: sha256:...
      normalizedPlanHash: sha256:...
    ingest:
      graphHash: sha256:...
      normalizedPlanHash: sha256:...

  locks:
    semantic: graphblocks.lock
    python: pylock.toml
    rust: Cargo.lock
    prompts: prompts.lock
    policies: policies.lock

  images:
    control: registry.example.com/gb/control@sha256:...
    docCpu: registry.example.com/gb/doc-cpu@sha256:...
    ocrGpu: registry.example.com/gb/ocr-gpu@sha256:...

  knowledge:
    indexRevision: intranet_docs_v17
    embeddingProfile: company-embedding-v4

  schemas:
    checkpoint: company.ai/Checkpoint@4
    conversation: company.ai/Conversation@3
    manifest: company.ai/IngestionManifest@2

  supplyChain:
    sbomRef: oci://registry/.../sbom@sha256:...
    provenanceRef: oci://registry/.../provenance@sha256:...
    signaturePolicy: production-publishers
```

Production release는 `latest`, Git branch, mutable prompt label, mutable image tag, unpinned index revision을 포함해서는 안 된다.

## 244. GraphDeployment

GraphDeployment는 environment의 desired state다.

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: GraphDeployment
metadata:
  name: enterprise-rag-production

spec:
  releaseRef:
    digest: sha256:...

  profile: production
  bindingRef: bindings/company-ai-production.yaml
  observabilityProfileRef: observability/rag-production.yaml

  coordinator:
    target: control

  targets: {}
  executionGroups: {}
  placements: []
  rollout: {}
  upgrades: {}
  recovery: {}
```

GraphDeployment에는 secret 값이 아니라 reference만 포함한다.

## 245. DeploymentRevision과 run pinning

Deployment controller/compiler는 GraphDeployment와 binding/cluster capability를 resolve해 불변 revision을 만든다.

```python
class DeploymentRevision(BaseModel):
    revision_id: str
    release_digest: str
    deployment_spec_hash: str
    physical_plan_hash: str
    resolved_binding_hash: str
    target_capability_hash: str
    created_at: datetime
```

권장 pin scope:

| workload | 기본 pin 범위 |
|---|---|
| HTTP request | run |
| chat | turn |
| sticky conversation | conversation |
| realtime voice | session |
| ingestion | job |
| map item | parent job revision 상속 |

실행 중 revision이 자동으로 바뀌면 안 된다.

## 246. PhysicalExecutionPlan

```yaml
apiVersion: graphblocks.ai/physical-plan/v1alpha1
plan:
  releaseDigest: sha256:...
  deploymentRevisionId: rev_...
  graphHash: sha256:...
  packageLockHash: sha256:...

  groups:
    chat-turn:
      target: control
      locality: same_process
      implementations:
        load_context: rust_builtin
        rewrite: python_inproc
        generate: python_inproc

    document-transform:
      target: doc-cpu
      locality: same_worker_per_invocation

    gpu-ocr:
      target: ocr-gpu
      locality: any_worker

  remoteEdges:
    - from: document-transform.convert
      to: gpu-ocr.ocr
      schema: graphblocks.ai/ArtifactRef@1
      transport: gb-worker-rpc
      delivery: at_least_once
```

Plan hash를 run, trace, manifest, checkpoint에 기록한다.

## 247. ExecutionTarget

```yaml
targets:
  control:
    kind: service
    executionHost: rust
    image: registry.example.com/gb/control@sha256:...
    packageLock: locks/control.lock
    accepts:
      capabilities:
        - graph.coordinator
        - model.remote_call
        - retrieval.remote_call

  doc-cpu:
    kind: workerPool
    executionHost: python_worker
    image: registry.example.com/gb/doc-cpu@sha256:...
    packageLock: locks/doc-cpu.lock
    accepts:
      capabilities:
        - document.parse.pdf
        - document.parse.office
        - document.normalize
        - document.split

  ocr-gpu:
    kind: workerPool
    executionHost: python_worker
    image: registry.example.com/gb/ocr-gpu@sha256:...
    accepts:
      capabilities:
        - document.ocr
        - accelerator.cuda

  sandbox:
    kind: sandboxPool
    executionHost: python_worker
    accepts:
      effects:
        - process_execution
        - workspace_write
```

Target는 정확한 Pod/Node가 아니라 logical worker pool이다.

## 248. ExecutionGroup과 locality

블록마다 Pod 하나를 생성하지 않는다. Remote boundary를 줄이기 위해 group을 사용한다.

```yaml
executionGroups:
  chat-turn:
    nodes: [load_context, classify, rewrite, build_context, generate, validate, commit]
    target: control
    locality: same_process

  per-document:
    subgraph: graphs/process-single-asset.yaml
    target: doc-cpu
    locality: same_worker_per_invocation
    dispatch: per_map_item

  gpu-ocr:
    nodes: [ocr]
    target: ocr-gpu
    locality: any_worker
```

Locality:

```text
same_process
same_worker_per_invocation
same_node_preferred
same_zone_required
any_worker
external
```

## 249. Placement rule

```yaml
placements:
  - select:
      nodes: [generate, build_context]
    target: control

  - select:
      capabilities: [document.parse.*]
    target: doc-cpu

  - select:
      blocks: [document.ocr]
    target: ocr-gpu

  - select:
      effects: [process_execution, workspace_write]
    target: sandbox
```

우선순위:

```text
node ID > execution group/subgraph > block ID > capability > execution class > default
```

동일 우선순위 충돌은 compile error다. Block requirement와 deployment overlay가 모두 만족되어야 한다.

## 250. Cross-target edge

Remote edge는 다음을 정의한다.

```text
wire schema/version
inline vs artifact_ref
payload limit/compression/checksum
delivery/retry/idempotency
cancellation/trace propagation
authentication/authorization/backpressure
```

대용량 file/document는 target 간 inline 복사보다 `ArtifactRef`를 사용한다.

```yaml
remoteEdges:
  - from: convert.document
    to: ocr.document
    transport:
      mode: artifact_ref
      binding: artifacts
      compression: zstd
      checksum: sha256
      delivery: at_least_once
```

## 251. Kubernetes mapping

| Target kind | Kubernetes workload |
|---|---|
| `service` | Deployment + Service |
| `workerPool` | Deployment |
| `jobPool` | Job/Indexed Job |
| `sandboxPool` | isolated Deployment 또는 invocation Job |
| `statefulService` | StatefulSet |
| `external` | 생성하지 않음 |

Portable fields가 기본이며 Kubernetes-specific overlay는 escape hatch다.

```yaml
targets:
  ocr-gpu:
    resources:
      requests:
        cpu: "4"
        memory: 16Gi
        accelerator:
          nvidia.com/gpu: 1

    platform:
      kubernetes:
        namespace: graphblocks-workers
        serviceAccountName: graphblocks-ocr
        nodeSelector:
          workload.graphblocks.ai/class: gpu
        tolerations:
          - key: nvidia.com/gpu
            operator: Exists
            effect: NoSchedule
        topologySpread:
          topologyKey: topology.kubernetes.io/zone
          maxSkew: 1
```

Gateway API를 신규 route exposure 기본으로 사용하고 Ingress는 compatibility option으로 둔다.

## 252. Sandbox와 network boundary

```yaml
targets:
  sandbox:
    kind: sandboxPool
    security:
      trustLevel: untrusted
      filesystem: ephemeral
      rootFilesystem: read_only
      privilegeEscalation: denied
      egressPolicy: restricted
    platform:
      kubernetes:
        runtimeClassName: gvisor
        serviceAccountName: graphblocks-sandbox
```

Deployment renderer는 NetworkPolicy, service account, pod security profile, secret mount 정책을 생성하거나 요구사항으로 출력할 수 있다.

## 253. Worker lifecycle와 draining

Worker state:

```text
STARTING → WARMING → READY ↔ SATURATED
READY/SATURATED → DRAINING → TERMINATED
READY → DEGRADED | UNHEALTHY
```

Probe 의미:

```text
startup   package/plugin/schema/model warmup 완료
readiness 새 task를 받을 수 있고 registry/queue capacity가 유효
liveness  runtime loop/heartbeat가 살아 있고 deadlock이 없음
```

외부 provider 장애만으로 liveness를 실패시켜 Pod를 재시작하지 않는다.

Drain sequence:

```text
readiness false
→ worker registry DRAINING
→ new lease 거부
→ active task 완료 또는 checkpoint
→ incremental output 종료
→ required outbox flush
→ telemetry bounded flush
→ lease 반환
→ exit
```

```yaml
lifecycle:
  drain:
    onlineRequestTimeout: 30s
    durableTaskTimeout: 5m
    realtimeSessionTimeout: 10m
    onDeadline:
      onlineRequest: cancel
      durableTask: checkpoint
      realtimeSession: disconnect_with_resume_token
```

## 254. Autoscaling, admission, load shedding

```yaml
targets:
  control:
    scaling:
      kind: request
      minReplicas: 3
      maxReplicas: 20
      targetConcurrencyPerReplica: 32

  doc-cpu:
    scaling:
      kind: queue
      minReplicas: 0
      maxReplicas: 40
      targetQueueDepthPerReplica: 4

admission:
  maxConcurrentRuns: 500
  maxQueueWait: 2s
  overload:
    strategy: reject
    retryAfter: 2s
```

Worker admission policy and selection helpers MUST validate their public input contracts before
evaluating readiness. Admission policies require a non-negative integer protocol version and
non-empty optional package-lock/block requirements. Admission and evaluation helpers require typed
`WorkerAdmissionPolicy` and `WorkerAdvertisement` records. Worker selection requires an iterable of
typed worker advertisements and a non-empty block identity, and must fail with a protocol error for
malformed inputs instead of relying on incidental language exceptions.

Scaling signal은 workload별로 다르다.

```text
online: concurrency, queue wait, TTFT
batch: queue depth, oldest item age, throughput
GPU: active model slots, memory, queue age
realtime: active sessions; scale-to-zero 금지 가능
```

## 255. Workload-aware rollout

공통 전략:

```text
validate → shadow → canary/blue-green → promote 또는 abort
```

```yaml
rollout:
  strategy: canary
  affinity: conversation_id
  steps:
    - traffic: 1
      minimumSamples: 200
    - traffic: 10
      minimumDuration: 30m
    - traffic: 50
      minimumDuration: 1h
  analysisProfile: rag-production-rollout
```

Workload별 규칙:

- Chat: 한 turn 중 revision 변경 금지; conversation sticky policy 명시.
- Ingestion: fixture regression → production sample shadow → staging index dual-write → alias publish.
- Effectful agent: shadow에서 effect suppress/sandbox; 비가역 effect는 자동 rollback 대상이 아니다.
- Realtime session: 기존 session drain, 신규 session만 새 revision.

RAG release는 graph, prompt, embedding profile, index revision을 하나의 cohort로 rollout한다.

## 256. Upgrade, migration, rollback

```yaml
upgrades:
  existingRequests: finish_on_old
  conversations: keep_affinity
  durableJobs: checkpoint_and_migrate
  realtimeSessions: drain_on_old
```

Compatibility matrix:

```text
runtime protocol
plan format
checkpoint schema
RunStore/ConversationStore/Manifest schema
worker package lock
canonical schema migrations
```

Rollback class:

```text
runtime/image rollback
prompt/graph rollback
index alias rollback
state migration rollback
compensation graph for effects
non-reversible effect
```

자동 rollback이 non-reversible effect를 되돌린다고 가정해서는 안 된다.

## 257. Control plane HA와 fencing

```python
class RunOwnershipLease(BaseModel):
    run_id: str
    owner_instance_id: str
    lease_epoch: int
    expires_at: datetime
    last_checkpoint: str | None = None
```

규칙:

- 한 run에는 하나의 active owner만 존재한다.
- ownership acquire는 fencing epoch를 발급한다.
- stale owner의 state/effect result write를 거부한다.
- worker result는 lease epoch와 node attempt ID를 포함한다.
- owner 장애 시 compatible checkpoint 이후부터 재개한다.

Worker result validation MUST first require typed `WorkerInvokeRequest` and `WorkerInvokeResult`
records, then compare invocation ID, node attempt ID, and lease epoch. Malformed validation inputs
must fail as worker result validation errors rather than incidental attribute errors.

Worker advertisement:

```python
class WorkerAdvertisement(BaseModel):
    worker_id: str
    target_id: str
    protocol_versions: list[str]
    package_lock_hash: str
    image_digest: str
    capabilities: set[str]
    state: str
    heartbeat_at: datetime
```

## 258. Multi-tenancy, residency, recovery

지원 isolation profile:

```text
shared_runtime
dedicated_worker_pool
namespace_isolated
cluster_isolated
region_isolated
```

```yaml
tenancy:
  mode: dedicated_worker_pool
  policyProfileRef: tenant-standard
  quotaDefaults:
    maxConcurrentRuns: 100
    modelInputTokensPerDay: 10000000
    artifactStorage: 100Gi
  network:
    defaultEgress: deny
```

Recovery profile은 RPO/RTO, backup source, restore compatibility, failover ownership을 정의한다.

```yaml
recovery:
  service:
    rto: 15m
    rpo: 5m
  durableJobs:
    rto: 1h
    rpo: checkpoint
  knowledgeIndex:
    rebuildableFrom: [source_assets, manifests, release_bundle]
  regionalFailover:
    mode: active_passive
```

정기 restore test는 production acceptance criterion이다.

## 259. Terraform와 GitOps 경계

Terraform 책임:

```text
cluster/node pool/network/IAM
object store/database/queue/search service
workload identity/DNS/certificate
GraphBlocks operator/Helm release
```

GraphBlocks 책임:

```text
portable infrastructure requirement
module input/tfvars generation
Terraform output → BindingSpec import
release/deployment manifest
runtime scheduling/retry/cancellation
```

GraphBlocks가 임의 HCL 전체를 source of truth로 생성하지 않는다.

```bash
graphblocks infra requirements deployment.yaml \
  --format terraform-vars \
  --out graphblocks.auto.tfvars.json

graphblocks bindings import \
  --from terraform-output.json \
  --template bindings/production.template.yaml
```

Secret 값은 Terraform output이나 generated BindingSpec에 기록하지 않고 SecretRef만 연결한다.

GitOps repository에는 mutable source가 아니라 release digest와 GraphDeployment desired state를 기록한다.

## 260. Software supply chain

Production release는 다음을 지원해야 한다.

```text
image and bundle digest
SBOM
build provenance
signature verification
plugin allowlist
vulnerability/license scan
package lock verification
admission policy
```

미검증 plugin/native image는 production target에 배치하지 않는다.

## 261. Deployment status

```python
class DeploymentStatus(BaseModel):
    observed_revision: str
    desired_release: str
    stable_revision: str | None
    canary_revision: str | None
    phase: str
    conditions: list[Condition]
    target_status: dict[str, TargetStatus]
    rollout_status: RolloutStatus | None
    migration_status: MigrationStatus | None
```

Condition 예:

```text
ReleaseVerified
BindingsResolved
PackagesAvailable
WorkersCompatible
MigrationsReady
RolloutHealthy
SLOWithinBudget
RecoveryTestCurrent
```

## 262. Deployment diagnostics와 CLI

```text
GB3001 NoCompatibleTarget
GB3002 AmbiguousPlacement
GB3003 MissingPackage
GB3004 UnsupportedExecutionHost
GB3005 AcceleratorUnavailable
GB3006 NonSerializableRemoteEdge
GB3007 OversizedInlineTransfer
GB3008 NonIdempotentRemoteEffect
GB3009 DataResidencyViolation
GB3010 LocalStorageViolation
GB3011 IsolationViolation
GB3012 RealtimeScaleToZero
GB3013 CyclicLocalityConstraint
GB4001 MutableReleaseReference
GB4002 UnverifiedArtifact
GB4003 IncompatibleCheckpointSchema
GB4004 UnsafeInFlightUpgrade
GB4005 MissingDrainPolicy
GB4006 RolloutWithoutQualityGate
GB4007 NonReversibleEffectRollback
```

```bash
graphblocks release build release.yaml --out dist/company-ai.gbr
graphblocks release verify dist/company-ai.gbr
graphblocks deploy plan deployment.yaml
graphblocks placement explain deployment.yaml --node ocr
graphblocks deploy render deployment.yaml --target kubernetes
graphblocks deploy render deployment.yaml --target helm
graphblocks deploy diff deployment.yaml --cluster production
graphblocks deploy doctor deployment.yaml
graphblocks images build deployment.yaml
graphblocks packages closure deployment.yaml
```
