# GraphBlocks Examples

Each example has an independently runnable contract, a deterministic integration
fixture, a short guide, and an integration test. A runner first validates every
YAML document, then executes the example through input-aware mock blocks, local
custom workers, and/or the same semantic acceptance gates used for conformance.
The harness blocks socket access, so model providers, databases, webhook
targets, exporters, and deployment systems are exercised through recording
fakes rather than real network calls. The custom-block example executes its
checked-in Python and Rust implementations directly. Examples 04, 13, and 14
also use the composition contract: their root graphs declare typed slots while
reusable workflow stages live in `fragments/` and are materialized before the
same validation and integration path runs.

| Example | Contract focus |
| --- | --- |
| [01-1 Enterprise RAG — YAML](01-enterprise-federated-rag/1-1-yaml-runtime/README.md) | YAML graph through the CLI runtime |
| [01-2 Enterprise RAG — Python](01-enterprise-federated-rag/1-2-python-runtime/README.md) | Python graph through `InProcessRuntime` |
| [01-3 Enterprise RAG — Rust](01-enterprise-federated-rag/1-3-rust-runtime/README.md) | Rust graph through `graphblocks-runtime-core` |
| [02 Marker document ingestion](02-document-ingestion/README.md) | Marker-first PDF parsing, fallback, ACL lineage |
| [03 Policy-governed chat](03-policy-governed-chat/README.md) | bounded completion and hard-stop profiles |
| [04 TUI workspace assistant](04-tui-workspace-assistant/README.md) | composed workspace/agent stages, TUI boundary, GPT/Gemini/Claude bindings |
| [05 Authority-backed advisory](05-authority-backed-advisory/README.md) | sources, evidence, review, and gated result |
| [06 Bounded research orchestrator](06-bounded-research-orchestrator/README.md) | task limits, budget delegation, replan CAS |
| [07 Verified RTL workspace trial](07-verified-rtl-workspace-trial/README.md) | trial checks, leases, review, governed commit |
| [08 Kubernetes production deployment](08-kubernetes-production-deployment/README.md) | release, placement, canary, rollback, drain |
| [09 Observability profile](09-observability-profile/README.md) | OTel/Langfuse projections and outage isolation |
| [10 Realtime voice extension](10-realtime-voice-extension/README.md) | duplex voice, interruption authority, playback |
| [11 OpenCode-style coding agent](11-coding-agent-background-callbacks/README.md) | AGENTS discovery, tool permission gates, replay, signed callbacks |
| [12 Custom Python and Rust blocks](12-custom-python-rust-blocks/README.md) | explicit registry, worker protocol, cross-language execution |
| [13 LLM interviewer RAG benchmark](13-llm-interviewer-rag-benchmark/README.md) | composed setup/variants/evaluation, blinded RAG vs no-RAG scoring |
| [14 vLLM config benchmark](14-vllm-config-benchmark/README.md) | composed collection/evaluation, TTFT and token-throughput comparison |

After the [development install](../docs/getting-started/installation.md), run one
example from the repository root:

```bash
python examples/01-enterprise-federated-rag/run.py
```

To inspect a composed example as the ordinary graph and imported resources that
the compiler and runtime receive:

```bash
graphblocks compose examples/14-vllm-config-benchmark/example.yaml
```

Run every example integration test with:

```bash
python -m pytest examples/*/test_*.py
```

The JSON result includes executed checks, mocked boundaries, input-bound call
evidence, and a canonical evidence digest. Composed examples also include the
expanded slot instances, imported source digests, and composition digest used by
the integration run. Examples are non-normative; schemas, the living
specification, TCK, and acceptance applications define conformance.
