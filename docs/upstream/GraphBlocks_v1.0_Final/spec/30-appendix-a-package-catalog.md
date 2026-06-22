# Appendix A. Package Catalog

## A.1 Core release train

| Distribution | Import | Type | Default install | Primary responsibility |
|---|---|---|---|---|
| `graphblocks-core` | `graphblocks` | pure Python | yes, via meta | schemas, GraphSpec, SDK |
| `graphblocks-runtime` | `graphblocks_runtime` | native wheel | yes, via meta | Rust execution engine |
| `graphblocks-stdlib` | `graphblocks_stdlib` | Python | yes, via meta | provider-neutral blocks |
| `graphblocks` | none/meta | metapackage | primary install | common provider-neutral install |
| `graphblocks-documents` | `graphblocks_documents` | Python | yes, via meta | document profile |
| `graphblocks-rag` | `graphblocks_rag` | Python | yes, via meta | retrieval/RAG |
| `graphblocks-conversation` | `graphblocks_conversation` | Python | yes, via meta | chat/session state |
| `graphblocks-policy` | `graphblocks_policy` | Python | yes, via meta | policy composition, PEP, default evaluator |
| `graphblocks-budget` | `graphblocks_budget` | Python | yes, via meta | budget/quota SPI and local ledger |
| `graphblocks-usage` | `graphblocks_usage` | Python | yes, via meta | usage facts and local ledger |
| `graphblocks-agents` | `graphblocks_agents` | Python | optional | tools/agent loop |
| `graphblocks-evaluation` | `graphblocks_evaluation` | Python | optional | check/metric/gate/trial |
| `graphblocks-orchestration` | `graphblocks_orchestration` | Python | optional | TaskPlan and budget delegation |
| `graphblocks-review` | `graphblocks_review` | Python | optional | immutable-subject review workflow |
| `graphblocks-workspace` | `graphblocks_workspace` | Python | optional | snapshot/ChangeSet/CAS workspace |
| `graphblocks-cli` | `graphblocks_cli` | Python/native helper | yes, via meta | CLI |
| `graphblocks-server` | `graphblocks_server` | Python | optional | HTTP/SSE/WebSocket |
| `graphblocks-worker` | `graphblocks_worker` | Python | optional | isolated Python execution |
| `graphblocks-devtools` | `graphblocks_devtools` | Python | dev | visualization/migration/codegen |
| `graphblocks-testing` | `graphblocks_testing` | Python | dev/test | deterministic runtime/TCK |

## A.2 Initial official integrations

| Category | Priority packages |
|---|---|
| Model | `graphblocks-openai`, `graphblocks-anthropic`, `graphblocks-google-genai` |
| Converter | `graphblocks-pypdf`, `graphblocks-docling`, `graphblocks-hwp` |
| Blob | `graphblocks-s3`, `graphblocks-gcs` |
| Knowledge | `graphblocks-qdrant`, `graphblocks-pgvector`, `graphblocks-opensearch` |
| State/record | `graphblocks-postgres`, `graphblocks-firestore`, `graphblocks-redis` |
| Observability | `graphblocks-langfuse`, `graphblocks-otel`, `graphblocks-prometheus` |
| Policy | `graphblocks-policy-opa`, `graphblocks-policy-cedar` |
| Durable ledger | `graphblocks-budget-postgres`, `graphblocks-usage-postgres` |
| Framework | `graphblocks-haystack`, `graphblocks-langgraph`, `graphblocks-langchain` |

## A.3 Optional extensions

| Extension | Packages |
|---|---|
| Voice | `graphblocks-voice`, `graphblocks-webrtc`, `graphblocks-websocket-media`, `graphblocks-openai-realtime`, `graphblocks-silero-vad` |
| Durable stream | `graphblocks-durable`, `graphblocks-kafka`, `graphblocks-nats`, `graphblocks-sqs`, `graphblocks-pubsub` |

