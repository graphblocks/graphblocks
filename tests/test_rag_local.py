from __future__ import annotations

from dataclasses import replace

import pytest

from graphblocks.documents import create_local_text_revision, parse_plain_text_document, chunk_document_by_lines
from graphblocks.rag import (
    AuthContext,
    InMemoryChunkRetriever,
    InMemoryKnowledgeIndex,
    KnowledgeIndexError,
    authorize_search_hits,
    knowledge_item_from_chunk,
)


def test_knowledge_item_from_chunk_preserves_chunk_source_ref() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\n")
    chunk = chunk_document_by_lines(document, revision, max_elements=1)[0]

    item = knowledge_item_from_chunk(chunk)

    assert item.item_id == chunk.chunk_id
    assert item.item_kind == "document_chunk"
    assert item.source == chunk.source_refs[0]
    assert item.metadata["document_id"] == document.document_id
    assert item.metadata["asset_id"] == asset.asset_id


def test_in_memory_chunk_retriever_returns_ranked_hits_with_lineage() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\nbeta gamma\nunrelated\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nbeta gamma\nunrelated\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    retriever = InMemoryChunkRetriever(chunks, retriever_id="local-test")

    hits = retriever.search("beta", top_k=2)

    assert [hit.rank for hit in hits] == [1, 2]
    assert [hit.item.item_id for hit in hits] == [chunks[0].chunk_id, chunks[1].chunk_id]
    assert hits[0].normalized_score == 1.0
    assert hits[0].retriever == "local-test"
    assert hits[0].highlights[0] == chunks[0].source_refs[0]


def test_in_memory_chunk_retriever_returns_empty_for_blank_query() -> None:
    retriever = InMemoryChunkRetriever([], retriever_id="local-test")

    assert retriever.search("   ") == []


def test_in_memory_knowledge_index_upserts_chunks_and_exposes_retriever_view() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/index.txt",
        "alpha beta\nrestricted beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nrestricted beta\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    index = InMemoryKnowledgeIndex("knowledge-local")

    report = index.upsert_chunks(chunks)
    result = index.retriever("knowledge-local-read").search("beta", top_k=10)

    assert report.operation == "upsert"
    assert report.affected_count == 2
    assert report.chunk_ids == [chunk.chunk_id for chunk in chunks]
    assert report.metadata == {"index_id": "knowledge-local"}
    assert [hit.item.item_id for hit in result] == [chunks[0].chunk_id, chunks[1].chunk_id]
    assert result[0].item.acl == revision.acl


def test_in_memory_knowledge_index_tombstones_without_returning_deleted_chunks() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/delete.txt",
        "alpha beta\nbeta gamma\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nbeta gamma\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    index = InMemoryKnowledgeIndex("knowledge-local")
    index.upsert_chunks(chunks)

    report = index.delete_asset(asset.asset_id, "tombstone")
    result = index.retriever("knowledge-local-read").search("beta", top_k=10)

    assert report.operation == "delete"
    assert report.affected_count == 2
    assert report.metadata == {"asset_id": asset.asset_id, "delete_mode": "tombstone"}
    assert result == []
    assert index.record(chunks[0].chunk_id).status == "tombstoned"
    assert index.health().tombstoned_chunks == 2


def test_in_memory_knowledge_index_hard_delete_removes_records() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/hard-delete.txt",
        "alpha beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    index = InMemoryKnowledgeIndex("knowledge-local")
    index.upsert_chunks(chunks)

    report = index.delete_asset(asset.asset_id, "hard")

    assert report.affected_count == 1
    assert index.record(chunks[0].chunk_id) is None
    assert index.health().indexed_chunks == 0


def test_in_memory_knowledge_index_updates_metadata_acl_and_publishes_revision() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/publish.txt",
        "alpha beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    chunk_id = chunks[0].chunk_id
    index = InMemoryKnowledgeIndex("knowledge-local")
    index.upsert_chunks(chunks)

    metadata_report = index.update_chunk_metadata(chunk_id, {"classification": "internal"})
    acl_report = index.update_chunk_acl(chunk_id, {"tenant_id": "acme", "principals": ["user-1"]})
    publish = index.publish_revision(asset.asset_id, revision.revision_id)
    hit = index.retriever("knowledge-local-read").search("beta", top_k=1)[0]

    assert metadata_report.operation == "update_metadata"
    assert metadata_report.metadata == {"metadata_keys": ["classification"]}
    assert acl_report.operation == "update_acl"
    assert publish.asset_id == asset.asset_id
    assert publish.revision_id == revision.revision_id
    assert publish.published_chunk_ids == [chunk_id]
    assert publish.metadata == {"active_chunk_count": 1}
    assert index.is_revision_published(asset.asset_id, revision.revision_id) is True
    assert index.capabilities().publish is True
    assert index.health().published_revisions == 1
    assert hit.item.metadata["classification"] == "internal"
    assert hit.item.acl == {"tenant_id": "acme", "principals": ["user-1"]}
    with pytest.raises(TypeError):
        hit.item.acl["principals"].append("user-2")


def test_in_memory_knowledge_index_reports_not_found_for_missing_item() -> None:
    index = InMemoryKnowledgeIndex("knowledge-local")

    try:
        index.update_chunk_metadata("missing", {"classification": "internal"})
    except KnowledgeIndexError as error:
        assert str(error) == "knowledge item 'missing' was not found"
    else:
        raise AssertionError("missing knowledge item should fail metadata update")


def test_authorize_search_hits_filters_protected_items_by_principal_group_role_and_tenant() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\nbeta gamma\nbeta delta\nbeta epsilon\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\nbeta gamma\nbeta delta\nbeta epsilon\n")
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    hits = InMemoryChunkRetriever(chunks, retriever_id="local-test").search("beta", top_k=4)
    hits[0] = replace(
        hits[0],
        item=replace(hits[0].item, acl={"tenant_id": "acme", "principals": ["user-1"]}),
    )
    hits[1] = replace(
        hits[1],
        item=replace(hits[1].item, acl={"tenant_id": "acme", "groups": ["support"]}),
    )
    hits[2] = replace(
        hits[2],
        item=replace(hits[2].item, acl={"tenant_id": "acme", "roles": ["agent"]}),
    )
    hits[3] = replace(
        hits[3],
        item=replace(hits[3].item, acl={"tenant_id": "other-tenant", "principals": ["user-1"]}),
    )

    authorized = authorize_search_hits(
        hits,
        AuthContext(tenant_id="acme", principal_id="user-1", groups={"support"}, roles={"agent"}),
    )

    assert [hit.hit_id for hit in authorized] == [hits[0].hit_id, hits[1].hit_id, hits[2].hit_id]


def test_authorize_search_hits_requires_auth_context_for_protected_items() -> None:
    asset, revision = create_local_text_revision(
        "file:///tmp/notes.txt",
        "alpha beta\n",
        observed_at="2026-06-22T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, "alpha beta\n")
    chunk = chunk_document_by_lines(document, revision, max_elements=1)[0]
    hit = InMemoryChunkRetriever([chunk], retriever_id="local-test").search("beta", top_k=1)[0]
    hit = replace(hit, item=replace(hit.item, acl={"tenant_id": "acme", "groups": ["support"]}))

    try:
        authorize_search_hits([hit], None)
    except PermissionError as error:
        assert str(error) == f"authorization context required for {hit.hit_id!r}"
    else:
        raise AssertionError("protected hits require an auth context")
