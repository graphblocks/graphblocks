# Documents and Retrieval

## Files, documents, and lineage

Binary content MUST be referenced by a content-bound artifact or blob identity.
File ingestion MUST enforce size, media type, tenant, and access policy before a
parser observes content. A document revision binds source identity, checksum,
parser lock, metadata, and ACL. Chunks MUST retain document/revision lineage,
ordered offsets, and inherited access controls through indexing.

A local blob store MUST resolve both content and sidecar metadata paths beneath
its configured root before writing either file. Parent traversal and symlink
resolution MUST NOT redirect metadata or content outside that boundary. The
reference and native stores MUST perform this confinement before either write so
a rejected key cannot leave a partial blob behind.
When sidecar metadata records a content checksum and size, local `head` and
`get` operations MUST verify the stored bytes against both values. Malformed
sidecars or mismatched content MUST fail closed as blob-store errors.

The Python local store writes version 2 sidecars beneath the reserved sibling
directory `.graphblocks-metadata-v2`, sharded by the SHA-256 digest of the exact
UTF-8 object key. Sidecars retain the original key in `artifact.artifact_id` and
MUST verify that identity against the requested key. Object path prefixes such as
`alpha` and `alpha.json/child` therefore do not create sidecar file/directory
collisions. The directory has a `.format` marker; an existing nonempty directory
without that marker MUST be rejected without changing its contents. Both
`.graphblocks-metadata` and `.graphblocks-metadata-v2` are reserved object-key
namespaces. The Python store still reads version 1 sidecars and recovers matching
version 1 pending metadata; new writes use version 2. This is a Python storage
format, not a promise that native stores share an on-disk layout.

Python version 2 store instances sharing a root serialize complete operations
with a process-local lock and an operating-system advisory lock on `.format`
(POSIX file lock or Windows byte-range lock). This includes reads, pending
metadata recovery, and listing, so another cooperating process cannot observe
or replace an in-progress writer's pending files. The operating system releases
the lock when a process exits. An empty marker left by interrupted initialization
may be initialized only when the directory has no other entries. Older Python
implementations, native stores, and direct filesystem writers do not participate
in this protocol and MUST NOT write concurrently to the same root.

The Python rename/pending protocol supports recovery after process interruption.
It does not promise persistence across an operating-system crash or power loss:
files and parent directories are not flushed with `fsync`.

Numeric cursors emitted or accepted by a local blob store MUST use canonical
unsigned decimal syntax. Advancing a valid cursor MUST NOT overflow, including
when a caller supplies the platform's largest representable cursor.

A zero-length blob byte-range read MUST verify that the blob exists and return
empty bytes without issuing an invalid transport range whose end precedes its
start. Local and S3-compatible stores MUST expose the same result.

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
