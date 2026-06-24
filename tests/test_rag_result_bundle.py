from __future__ import annotations

from graphblocks.evaluation import ResultBundle
from graphblocks.rag import (
    Answer,
    ContextPack,
    QueryPlan,
    RagResultBundle,
    RagResultPayload,
    RetrievalResult,
    SearchRequest,
)


def test_rag_result_bundle_wraps_generic_result_bundle_with_typed_payload() -> None:
    query_plan = QueryPlan(
        original="How do I reset a password?",
        rewritten=["reset password"],
        subqueries=["password reset policy"],
        filters={"tenant": "acme"},
        rationale_summary="normalized support request",
    )
    retrieval = RetrievalResult(
        retrieval_id="retrieval-1",
        request=SearchRequest("reset password", top_k=3),
        hits=[],
        total_candidates=0,
    )
    context = ContextPack(context_id="context-1", hits=[], token_budget=32, token_count=0)
    answer = Answer(answer_id="answer-1", text="Use the password reset flow.")
    payload = RagResultPayload(
        query_plan=query_plan,
        retrievals=[retrieval],
        context=context,
        model_response={"response_id": "response-1"},
        answer=answer,
    )
    base = ResultBundle(bundle_id="bundle-1", run_id="run-1", release_id="release-1", inputs=[], outputs=[])

    bundle = RagResultBundle(base=base, payload=payload)

    assert bundle.profile == "rag"
    assert bundle.base.content_digest() == base.content_digest()
    assert bundle.payload.query_plan.rewritten == ["reset password"]
    assert bundle.payload.retrievals[0].retrieval_id == "retrieval-1"
    assert bundle.payload.context.context_id == "context-1"
    assert bundle.payload.answer.answer_id == "answer-1"

