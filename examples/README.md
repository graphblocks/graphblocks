# GraphBlocks Examples

Each example has an independently runnable contract, a deterministic integration
fixture, a short guide, and an integration test. A runner first validates every
YAML document, then executes the example through input-aware mock blocks, local
custom workers, and/or the same semantic acceptance gates used for conformance.
The harness blocks socket access, so model providers, databases, webhook
targets, exporters, and deployment systems are exercised through recording
fakes rather than real network calls. The custom-block example executes its
checked-in Python and Rust implementations directly.

| Example | Contract focus |
| --- | --- |
| [01 Enterprise federated RAG](01-enterprise-federated-rag/README.md) | federation, reranking, grounding, citations |
| [02 Marker document ingestion](02-document-ingestion/README.md) | Marker-first PDF parsing, fallback, ACL lineage |
| [03 Policy-governed chat](03-policy-governed-chat/README.md) | bounded completion and hard-stop profiles |
| [04 TUI workspace assistant](04-tui-workspace-assistant/README.md) | application protocol and TUI client boundary |
| [05 Authority-backed advisory](05-authority-backed-advisory/README.md) | sources, evidence, review, and gated result |
| [06 Bounded research orchestrator](06-bounded-research-orchestrator/README.md) | task limits, budget delegation, replan CAS |
| [07 Verified RTL workspace trial](07-verified-rtl-workspace-trial/README.md) | trial checks, leases, review, governed commit |
| [08 Kubernetes production deployment](08-kubernetes-production-deployment/README.md) | release, placement, canary, rollback, drain |
| [09 Observability profile](09-observability-profile/README.md) | OTel/Langfuse projections and outage isolation |
| [10 Realtime voice extension](10-realtime-voice-extension/README.md) | duplex voice, interruption authority, playback |
| [11 Coding-agent callbacks](11-coding-agent-background-callbacks/README.md) | accepted run, replay, signed webhook, async resume |
| [12 Custom Python and Rust blocks](12-custom-python-rust-blocks/README.md) | explicit registry, worker protocol, cross-language execution |

After the [development install](../docs/getting-started/installation.md), run one
example from the repository root:

```bash
python examples/01-enterprise-federated-rag/run.py
```

Run every example integration test with:

```bash
python -m pytest examples/*/test_*.py
```

The JSON result includes executed checks, mocked boundaries, input-bound call
evidence, and a canonical evidence digest. Examples are non-normative; schemas,
the living specification, TCK, and acceptance applications define conformance.
