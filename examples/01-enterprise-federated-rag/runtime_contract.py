from __future__ import annotations

from collections.abc import Mapping

from graphblocks.canonical import canonical_hash


EXPECTED_SEMANTIC_RESULT = {
    "answerId": "answer-key-rotation",
    "citations": ["citation-rotation", "citation-ticket"],
    "status": "grounded",
    "text": "Use the security console and obtain two approvals.",
}


def normalize_runtime_result(
    payload: Mapping[str, object],
    *,
    runtime: str,
    graph: Mapping[str, object],
) -> dict[str, object]:
    outputs = payload.get("outputs")
    journal = payload.get("journal")
    if payload.get("status") != "succeeded" or not isinstance(outputs, Mapping):
        raise RuntimeError(f"{runtime} runtime did not succeed")
    candidate = outputs.get("candidate")
    validation = outputs.get("validation")
    if not isinstance(candidate, Mapping):
        raise RuntimeError(f"{runtime} runtime did not produce a candidate")
    if not isinstance(validation, Mapping):
        raise RuntimeError(f"{runtime} runtime did not produce grounding evidence")
    grounding_ok = validation.get("ok")
    grounding_issues = validation.get("issues")
    if not isinstance(grounding_ok, bool) or not isinstance(grounding_issues, list):
        raise RuntimeError(f"{runtime} grounding evidence is invalid")
    citations = candidate.get("citations")
    if not isinstance(citations, list):
        raise RuntimeError(f"{runtime} candidate citations must be a list")
    citation_ids = []
    for citation in citations:
        if not isinstance(citation, Mapping) or not isinstance(
            citation.get("citationId"), str
        ):
            raise RuntimeError(f"{runtime} candidate citation is invalid")
        citation_ids.append(citation["citationId"])
    semantic_result = {
        "answerId": candidate.get("answerId"),
        "citations": citation_ids,
        "status": "grounded" if grounding_ok and not grounding_issues else "ungrounded",
        "text": candidate.get("text"),
    }
    succeeded_nodes: list[str] = []
    graph_hash = payload.get("graphHash")
    if isinstance(journal, list):
        for record in journal:
            if not isinstance(record, Mapping):
                continue
            record_payload = record.get("payload")
            if (
                record.get("kind") == "run_started"
                and isinstance(record_payload, Mapping)
                and isinstance(record_payload.get("graphHash"), str)
            ):
                graph_hash = record_payload["graphHash"]
            if record.get("kind") != "node_succeeded":
                continue
            if isinstance(record_payload, Mapping) and isinstance(
                record_payload.get("node"), str
            ):
                succeeded_nodes.append(record_payload["node"])
    evidence: dict[str, object] = {
        "graphHash": graph_hash or canonical_hash(dict(graph)),
        "grounding": {"issueCount": len(grounding_issues), "ok": grounding_ok},
        "runtime": runtime,
        "semanticResult": semantic_result,
        "status": payload.get("status"),
        "succeededNodes": succeeded_nodes,
    }
    return {**evidence, "evidenceDigest": canonical_hash(evidence)}
