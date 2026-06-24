from __future__ import annotations

from graphblocks.canonical import canonical_hash
from graphblocks.rag import build_abstention_answer, build_answer_from_model_response


def test_build_answer_from_model_response_preserves_structured_output_metadata() -> None:
    model_response = {
        "response_id": "response-1",
        "provider": "scripted",
        "model": "model-test",
        "finish_reason": "stop",
        "output_text": "Alpha policy requires audit logs.",
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "Alpha policy requires audit logs.",
                "citation_ids": ["cite-1"],
            }
        ],
    }

    answer = build_answer_from_model_response("answer-1", model_response)

    assert answer.answer_id == "answer-1"
    assert answer.text == "Alpha policy requires audit logs."
    assert [claim.claim_id for claim in answer.claims] == ["claim-1"]
    assert answer.claims[0].citation_ids == ["cite-1"]
    assert answer.metadata["model_response_digest"] == canonical_hash(model_response)
    assert answer.metadata["provider_response_id"] == "response-1"
    assert answer.metadata["provider"] == "scripted"
    assert answer.metadata["model"] == "model-test"
    assert answer.metadata["finish_reason"] == "stop"


def test_build_answer_from_model_response_requires_text() -> None:
    try:
        build_answer_from_model_response("answer-1", {"response_id": "response-1"})
    except ValueError as error:
        assert str(error) == "model_response must contain string output_text or text"
    else:
        raise AssertionError("answer assembly should require model output text")


def test_build_abstention_answer_sets_terminal_answer_and_diagnostics() -> None:
    answer = build_abstention_answer(
        "answer-1",
        "insufficient_context",
        "I do not have enough validated source support to answer.",
        diagnostics={"issue_codes": ["grounding.insufficient_context"]},
    )

    assert answer.answer_id == "answer-1"
    assert answer.text == "I do not have enough validated source support to answer."
    assert answer.claims == []
    assert answer.citations == []
    assert answer.abstention is not None
    assert answer.abstention.reason == "insufficient_context"
    assert answer.abstention.diagnostics == {
        "issue_codes": ["grounding.insufficient_context"]
    }
    assert answer.metadata["answer_kind"] == "abstention"
