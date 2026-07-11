# Documents and Retrieval

## Files, documents, and lineage

Binary content MUST be referenced by a content-bound artifact or blob identity.
File ingestion MUST enforce size, media type, tenant, and access policy before a
parser observes content. A document revision binds source identity, checksum,
parser lock, metadata, and ACL. Chunks MUST retain document/revision lineage,
ordered offsets, and inherited access controls through indexing.

A local blob store MUST resolve both content and sidecar metadata paths beneath
its configured root before writing either file. Parent traversal and symlink
resolution MUST NOT redirect metadata or content outside that boundary.

## Ordered parser fallback

A parser candidate chain MUST be non-empty, ordered, and free of duplicate
`(processor_id, version)` identities. Every attempted candidate MUST produce an
immutable lock that records candidate index, artifact checksum, and primary or
fallback reason. Failed locks remain ordered evidence. The selected lock MUST
not also appear among failures. If all candidates fail, processing is terminal;
configuration alone is not execution evidence.

## Retrieval and answers

Retrieval MUST apply tenant and principal authorization before returning
content. Federation and fusion MUST retain source/index identity, score
provenance, and deterministic tie behavior. Reranking MUST NOT restore a result
removed by authorization.

Freshness filtering MUST treat source modification metadata as a complete ISO
datetime. Fractional seconds, when present, MUST contain one digit-only
component; malformed timestamps MUST NOT satisfy a freshness threshold.

An answer claiming grounding MUST bind citations to returned chunks and validate
that source spans, lineage, and access decisions are present. Invalid or
insufficient context MUST produce explicit diagnostics and the configured
abstention behavior. An answer MUST NOT claim a citation merely because a source
identifier appeared in a prompt.
