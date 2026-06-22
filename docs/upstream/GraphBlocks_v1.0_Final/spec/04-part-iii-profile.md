# Part III. 파일 및 문서 처리 Profile

## 72. 범위

Document profile은 파일을 읽는 기능만이 아니라 다음 lifecycle 전체를 정의한다.

```text
acquire
→ identify revision
→ validate and classify
→ convert/OCR
→ canonicalize
→ normalize/enrich
→ split
→ index/write
→ publish
→ update/delete
```

문서 처리 결과는 RAG뿐 아니라 요약, 번역, 분류, extraction, diff, artifact generation에 재사용되어야 한다.

## 73. 파일 사용 유형

| 유형 | 예 | 기본 수명 | 기본 저장 |
|---|---|---|---|
| direct analysis | “이 PDF 요약” | turn/conversation | 임시 |
| temporary corpus | 여러 파일을 올린 프로젝트 대화 | conversation/project | TTL |
| permanent knowledge | 사내 규정/매뉴얼 | project/tenant | durable |
| batch transformation | 번역본/보고서 생성 | job | output policy |
| generated artifact | PDF/DOCX/XLSX 산출물 | explicit | artifact store |

GraphSpec은 file attachment의 목적과 수명을 명시해야 한다.

## 74. Source acquisition

표준 source block:

```text
asset.from_upload
asset.from_local
asset.from_http
asset.from_blob
asset.from_record
asset.discover
asset.watch
```

Source는 `SourceAsset`과 `AssetRevision`을 반환해야 한다. 단순 path string만 반환하면 revision, checksum, ACL lineage를 잃는다.

### Remote fetch policy

HTTP 및 cloud fetch는 다음을 지원해야 한다.

- size limit
- content-type allowlist
- redirect limit
- timeout
- checksum validation
- SSRF protection
- egress policy
- credential scope
- range read capability

## 75. File fingerprint와 revision

Fingerprint는 최소한 content hash를 포함한다.

```python
class FileFingerprint(BaseModel):
    algorithm: Literal["sha256", "blake3"]
    digest: str
    size_bytes: int
    normalized_source_uri: str | None = None
```

Metadata-only 변경과 content 변경을 구분해야 한다.

```text
content revision
metadata revision
ACL revision
processing revision
```

재처리 여부는 위 revision과 processor config hash를 함께 사용해 결정한다.

## 76. MIME/type detection

확장자만 신뢰하지 않는다.

```text
filename extension
+ declared media type
+ magic bytes
+ archive/container inspection
→ DetectionResult
```

```python
class DetectionResult(BaseModel):
    media_type: str
    confidence: float
    container_type: str | None = None
    warnings: list[str] = Field(default_factory=list)
```

암호화 PDF, macro-enabled Office file, archive bomb, executable 포함 문서는 별도 policy로 처리한다.

## 77. Archive와 container 처리

ZIP, email, Office container, HWPX 등은 nested asset을 만들 수 있다.

```text
parent asset
  ├─ embedded image
  ├─ attachment
  ├─ worksheet
  └─ nested document
```

규칙:

- traversal path(`../`)를 거부한다.
- depth, file count, expanded size를 제한한다.
- child asset은 parent lineage를 가진다.
- embedded asset마다 독립 retention/ACL을 적용할 수 있다.

## 78. Conversion strategy

`document.convert`는 provider-neutral semantic block이다.

```yaml
nodes:
  convert:
    block: document.convert@1
    config:
      strategy: auto
      preferredImplementations:
        - docling
        - pypdf
      fallback: provider_native
```

Conversion output은 `ParsedDocument` 또는 conversion failure다.

### Converter capability

```text
supported_media_types
text_extraction
layout
page_images
tables
formulas
ocr
embedded_assets
password_protected
streaming_pages
```

Compiler 또는 binder는 요구 capability와 converter capability를 비교해야 한다.

## 79. FileInputStrategy

모든 파일을 먼저 Markdown으로 변환할 필요는 없다.

```text
provider_native
parsed_full_text
parsed_multimodal
retrieve_from_index
code_analysis
```

선택 기준:

| 전략 | 적합한 경우 | 주의점 |
|---|---|---|
| `provider_native` | 일회성 PDF/이미지 분석 | provider 종속, 재현성 |
| `parsed_full_text` | 짧은 문서, 정형 텍스트 | layout 손실 가능 |
| `parsed_multimodal` | 표/그림/페이지 layout 중요 | 비용과 payload 크기 |
| `retrieve_from_index` | 대규모/반복 질문 | indexing latency |
| `code_analysis` | spreadsheet, archive, programmatic 분석 | sandbox 필요 |

`file.input.select_strategy` block이 정책과 capability를 바탕으로 선택할 수 있다.

## 80. OCR

OCR은 converter의 숨겨진 옵션이 아니라 독립 processor로 모델링할 수 있다.

```text
document.ocr
image.ocr
pdf.ocr_overlay
```

OCR 결과에는 다음 provenance가 필요하다.

```text
engine/model/version
language hints
page/region
confidence
rotation/deskew
preprocessing config hash
```

OCR text는 원본 text layer를 덮어쓰지 않고 source variant로 보존해야 한다.

## 81. Canonical document element

Parser가 반환한 provider-specific tree를 canonical `DocumentElement`로 변환한다.

### TableElement

```python
class TableElement(BaseModel):
    element_id: str
    rows: list[TableRow]
    caption: str | None = None
    header_rows: int = 0
    location: SourceLocation
```

Cell에는 row/column span과 원본 위치를 보존해야 한다.

### ImageElement

```python
class ImageElement(BaseModel):
    element_id: str
    artifact: ArtifactRef
    alt_text: str | None = None
    caption: str | None = None
    location: SourceLocation
```

Image description을 생성한 경우 model/version과 생성 여부를 metadata에 기록한다.

### Spreadsheet

Spreadsheet는 단일 텍스트 문서로 평탄화하지 않는다.

```text
Workbook
→ Sheet
→ SheetRegion / Table
→ Cell values and formulas
```

Cell range를 citation에 사용할 수 있어야 한다.

### Presentation

Slide 번호, shape order, speaker note, image와 text 관계를 보존한다.

## 82. Normalization

표준 block:

```text
document.normalize_unicode
document.remove_repeated_header_footer
document.normalize_whitespace
document.repair_hyphenation
document.normalize_lists
document.normalize_tables
document.detect_language
```

Normalization은 원본 element를 파괴하지 않고 transformed document와 processor lineage를 생성해야 한다.

## 83. Cleaning과 redaction

```text
document.clean
document.redact_pii
document.remove_boilerplate
document.policy_filter
```

Redaction 결과는 다음을 기록한다.

- redaction rule/model
- 원본 span reference
- replacement token
- reversible 여부
- audit reference

원본 restricted artifact와 redacted derivative는 별도 ACL을 가질 수 있다.

## 84. Enrichment

```text
document.title_extract
document.metadata_enrich
document.classify
document.entity_extract
document.keyword_extract
document.summary
document.language_detect
document.security_label
```

Enrichment는 `DocumentElement` 또는 `ParsedDocument`를 mutate하지 않고 새 revision 또는 annotation을 생성한다.

## 85. Splitter/Chunker

표준 전략:

```text
fixed_tokens
sentence
paragraph
section_aware
page_aware
layout_aware
table_aware
semantic
parent_child
```

```yaml
nodes:
  split:
    block: document.split@1
    config:
      strategy: section_aware
      targetTokens: 600
      maxTokens: 850
      overlapTokens: 80
      preserveTables: true
      parentChild:
        enabled: true
        parentTokens: 2200
```

필수 output:

- chunk ID
- source element IDs
- source spans
- chunker version/config hash
- token count와 tokenizer ref
- ACL/security labels

## 86. Parent-child와 hierarchical retrieval

큰 section과 작은 retrieval chunk를 함께 사용할 수 있다.

```text
Parent chunk: 문맥 보존
Child chunk: 검색 정밀도
```

SearchHit이 child를 반환한 뒤 context selector가 parent를 확장할 수 있다. Parent 확장은 ACL과 token budget을 다시 검증해야 한다.

## 87. Deduplication

중복은 여러 단계에서 처리한다.

```text
asset exact duplicate
near-duplicate document
repeated template/boilerplate
near-duplicate chunk
```

Dedup 결과는 삭제가 아니라 canonical reference와 duplicate relationship으로 기록하는 것이 기본이다.

```python
class DuplicateRelation(BaseModel):
    source_id: str
    canonical_id: str
    method: str
    score: float | None = None
```

## 88. Embedding

```text
embedding.document
embedding.text
embedding.multimodal
```

```python
class EmbeddingRecord(BaseModel):
    embedding_id: str
    source_id: str
    vector: list[float] | None = None
    dimension: int
    model: str
    model_revision: str | None = None
    config_hash: str
    created_at: datetime
```

Vector를 event log나 telemetry에 넣지 않는다. 저장 위치 reference만 기록한다.

## 89. Ingestion manifest

```python
class IngestionManifest(BaseModel):
    manifest_id: str
    asset_id: str
    revision_id: str
    source_uri: str
    content_hash: str

    parser: ProcessorRef
    ocr: ProcessorRef | None = None
    normalizers: list[ProcessorRef] = Field(default_factory=list)
    chunker: ProcessorRef
    embedding: ProcessorRef | None = None

    parsed_document_ref: ArtifactRef | None = None
    chunk_set_ref: ArtifactRef | None = None
    index_records: list[IndexRecordRef] = Field(default_factory=list)

    acl_revision: str | None = None
    pipeline_hash: str
    status: Literal[
        "discovered", "processing", "ready", "failed", "superseded", "deleted"
    ]
    error: BlockError | None = None
    created_at: datetime
    updated_at: datetime
```

Manifest는 dedupe, retry, rollback, deletion, reindex, audit, lineage의 source of truth다.

## 90. Processing cache

Cache key:

```text
content_hash
+ block type/version
+ implementation version
+ config hash
+ relevant policy hash
+ schema version
```

Secret 값은 cache key에 직접 넣지 않는다. 결과가 tenant/ACL에 의존하면 scope를 key에 포함한다.

## 91. Ingestion transaction

일반적인 commit sequence:

```text
1. create processing manifest
2. write derived artifacts
3. write chunks/embeddings
4. upsert index records to staging namespace
5. validate counts and ACL payload
6. commit manifest
7. publish index revision/alias
8. mark previous revision superseded
```

중간 실패가 current index를 부분 변경하지 않도록 staging 또는 generation ID를 권장한다.

## 92. Index version과 publish

```text
knowledge/hr-v1
knowledge/hr-v2
alias: knowledge/hr-current → hr-v2
```

```yaml
nodes:
  publish:
    block: knowledge.publish@1
    connection: knowledge
    config:
      alias: hr-current
      targetRevision: ${state.index_revision}
      strategy: atomic_alias_swap
```

Connector가 atomic alias를 지원하지 않으면 capability error 또는 명시적 non-atomic policy가 필요하다.

## 93. Update와 change propagation

변경 종류별 동작:

| 변경 | 기본 동작 |
|---|---|
| content 변경 | parse부터 재처리 |
| parser/chunker 변경 | 해당 단계 이후 재처리 |
| embedding model 변경 | embedding/index 재생성 |
| metadata 변경 | metadata/index payload update |
| ACL 변경 | chunk/index ACL 즉시 갱신 |
| source delete | tombstone 후 retention policy 적용 |

ACL 변경은 content re-embedding을 요구하지 않아야 하지만 retrieval filter에는 즉시 반영되어야 한다.

## 94. Delete와 tombstone

```yaml
nodes:
  load_manifest:
    block: manifest.get@1

  remove_index:
    block: knowledge.delete@1
    connection: knowledge

  tombstone:
    block: manifest.tombstone@1

  schedule_artifact_delete:
    block: blob.delete@1
    connection: artifacts
```

Deletion 요구사항:

- permission
- audit
- idempotency
- index record removal
- derived artifact policy
- cache invalidation
- citation dead-link policy
- legal hold 예외

## 95. Generated artifacts

Output file도 first-class data다.

```python
class GeneratedArtifact(BaseModel):
    artifact: ArtifactRef
    kind: Literal[
        "report", "translation", "extraction", "spreadsheet", "presentation", "archive"
    ]
    source_ids: list[str]
    generator: ProcessorRef
    provenance: dict[str, JsonValue]
```

표준 block:

```text
artifact.render_pdf
artifact.render_docx
artifact.render_pptx
artifact.render_xlsx
artifact.write_json
artifact.bundle
```

## 96. 표준 document block catalog

```text
asset.fetch
asset.discover
asset.fingerprint
asset.detect_type
asset.unpack

file.input.select_strategy

document.convert
document.ocr
document.normalize
document.clean
document.redact
document.enrich
document.classify
document.extract
document.split
document.deduplicate
document.diff
document.write

embedding.document
knowledge.upsert
knowledge.delete
knowledge.publish
manifest.get
manifest.commit
manifest.tombstone
```

Provider 이름은 semantic block ID에 포함하지 않는다.

## 97. Single document ingestion 예

```yaml
apiVersion: graphblocks.ai/v1alpha3
kind: Graph
metadata:
  name: ingest-one-document
  version: 1.0.0

spec:
  profile: ingestion_job

  inputs:
    asset:
      type: graphblocks.ai/AssetRevision@1

  outputs:
    manifest:
      type: graphblocks.ai/IngestionManifest@1

  connections:
    artifacts: s3-artifacts
    knowledge: qdrant-knowledge
    manifests: postgres-manifests
    embedding: openai-embedding

  nodes:
    detect:
      block: asset.detect_type@1

    store_raw:
      block: blob.put@1
      connection: artifacts
      flow:
        retry: idempotent-write

    convert:
      block: document.convert@1
      config:
        strategy: auto
      flow:
        semaphore: document-convert
        timeout: 120s

    normalize:
      block: document.normalize@1

    split:
      block: document.split@1
      config:
        strategy: section_aware
        targetTokens: 600
        overlapTokens: 80

    embed:
      block: embedding.document@1
      connection: embedding

    upsert:
      block: knowledge.upsert@1
      connection: knowledge
      flow:
        retry: idempotent-write

    commit:
      block: manifest.commit@1
      connection: manifests

  edges:
    - from: $input.asset
      to: detect.asset
    - from: $input.asset
      to: store_raw.asset
    - from: $input.asset
      to: convert.asset
    - from: detect.result
      to: convert.detection
    - from: convert.document
      to: normalize.document
    - from: normalize.document
      to: split.document
    - from: split.chunks
      to: embed.documents
    - from: embed.documents
      to: upsert.documents
    - from: upsert.records
      to: commit.index_records
    - from: commit.manifest
      to: $output.manifest
```

## 98. Direct file analysis 예

```yaml
nodes:
  select_strategy:
    block: file.input.select_strategy@1
    config:
      maxNativeBytes: 20000000
      preferRetrievalAbovePages: 80

  analyze:
    block: model.chat@1
    connection: model

edges:
  - from: $input.message
    to: select_strategy.message
  - from: $input.attachments
    to: select_strategy.attachments
  - from: select_strategy.context
    to: analyze.context
```

Direct analysis 결과도 가능한 경우 `Citation`을 source page/cell에 연결한다.

## 99. Document processing quality metrics

```text
conversion_success_rate
text_coverage
layout_element_recall
table_structure_accuracy
ocr_character_error_rate
heading_preservation
chunk_size_distribution
chunk_source_span_coverage
duplicate_rate
index_write_success_rate
acl_payload_accuracy
delete_propagation_latency
```

Metric은 processor version과 fixture revision에 연결되어야 한다.

