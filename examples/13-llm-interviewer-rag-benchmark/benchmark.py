from __future__ import annotations

from decimal import Decimal
import json

from graphblocks.canonical import canonical_hash
from graphblocks.documents import (
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from graphblocks.evaluation import (
    GateConstraint,
    MetricObservation,
    ResourceSnapshotRef,
    TrialResult,
    evaluate_gate,
)
from graphblocks.integrations.scripted import ScriptedModelProvider
from graphblocks.rag import (
    ContextPack,
    InMemoryChunkRetriever,
    SearchRequest,
    render_context_pack,
)


INTERVIEW_PLAN_PROMPT = """You are an evaluation interviewer.
Create one factual interview question per reference fact. Return JSON only.

Reference facts:
- Refund requests are accepted within 30 days of purchase.
- Enterprise SSO uses SAML 2.0.
- Audit logs are retained for 365 days.
"""


def run_benchmark() -> dict[str, object]:
    question_fixtures = [
        {
            "questionId": "refund-window",
            "question": "What is the refund request window in days?",
            "referenceAnswer": "Refund requests are accepted within 30 days of purchase.",
            "ragAnswer": "Refund requests are accepted within 30 days of purchase.",
            "noRagAnswer": "Refund requests are usually accepted within 14 days.",
            "noRagScore": "0",
        },
        {
            "questionId": "enterprise-sso",
            "question": "Which protocol does Enterprise SSO use?",
            "referenceAnswer": "Enterprise SSO uses SAML 2.0.",
            "ragAnswer": "Enterprise SSO uses SAML 2.0.",
            "noRagAnswer": "SAML is common for enterprise SSO, but the protocol is not specified.",
            "noRagScore": "0.6",
        },
        {
            "questionId": "audit-retention",
            "question": "How many days are audit logs retained?",
            "referenceAnswer": "Audit logs are retained for 365 days.",
            "ragAnswer": "Audit logs are retained for 365 days.",
            "noRagAnswer": "Audit logs are retained for 90 days.",
            "noRagScore": "0",
        },
    ]
    plan_contract = {
        "questions": [
            {
                "questionId": fixture["questionId"],
                "question": fixture["question"],
                "referenceAnswer": fixture["referenceAnswer"],
            }
            for fixture in question_fixtures
        ]
    }
    planner = ScriptedModelProvider(
        scripts={
            INTERVIEW_PLAN_PROMPT: json.dumps(
                plan_contract,
                sort_keys=True,
                separators=(",", ":"),
            )
        },
        model="scripted-interviewer-v1",
        provider_id="scripted-interviewer",
    )
    plan_response = planner.generate(
        INTERVIEW_PLAN_PROMPT,
        response_id="interview-plan",
        metadata={"role": "interviewer", "stage": "question_generation"},
    )
    parsed_plan = json.loads(plan_response.text)
    if not isinstance(parsed_plan, dict) or not isinstance(parsed_plan.get("questions"), list):
        raise ValueError("interviewer plan must contain a questions list")
    questions = parsed_plan["questions"]
    if not questions:
        raise ValueError("interviewer plan must contain at least one question")

    chunks = []
    for index, fixture in enumerate(question_fixtures, start=1):
        fact = str(fixture["referenceAnswer"])
        asset, revision = create_local_text_revision(
            f"file:///benchmark/reference-{index}.txt",
            fact,
            observed_at="2026-07-13T00:00:00Z",
        )
        document = parse_plain_text_document(asset, revision, fact)
        chunks.extend(chunk_document_by_lines(document, revision, max_elements=1))
    retriever = InMemoryChunkRetriever(chunks, retriever_id="benchmark-reference")

    fixtures_by_id = {
        str(fixture["questionId"]): fixture for fixture in question_fixtures
    }
    prepared_cases: list[dict[str, object]] = []
    rag_scripts: dict[str, str] = {}
    no_rag_scripts: dict[str, str] = {}
    for raw_question in questions:
        if not isinstance(raw_question, dict):
            raise ValueError("interviewer questions must be mappings")
        question_id = raw_question.get("questionId")
        question = raw_question.get("question")
        reference_answer = raw_question.get("referenceAnswer")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (question_id, question, reference_answer)
        ):
            raise ValueError("interviewer question identity and text must be non-empty strings")
        fixture = fixtures_by_id.get(str(question_id))
        if fixture is None:
            raise ValueError(f"interviewer returned unknown question {question_id!r}")

        retrieval = retriever.retrieve(SearchRequest(str(question), top_k=1))
        if not retrieval.hits:
            raise ValueError(f"RAG candidate retrieved no context for {question_id!r}")
        context = ContextPack(
            context_id=f"context:{question_id}",
            hits=list(retrieval.hits),
        )
        rendered_context = render_context_pack(context)
        rag_prompt = (
            "Answer the interview question using only the context.\n"
            f"Question: {question}\n"
            f"{rendered_context}"
        )
        no_rag_prompt = (
            "Answer the interview question without retrieval.\n"
            f"Question: {question}"
        )
        rag_scripts[rag_prompt] = str(fixture["ragAnswer"])
        no_rag_scripts[no_rag_prompt] = str(fixture["noRagAnswer"])
        prepared_cases.append(
            {
                "questionId": str(question_id),
                "question": str(question),
                "referenceAnswer": str(reference_answer),
                "retrieval": retrieval,
                "ragPrompt": rag_prompt,
                "noRagPrompt": no_rag_prompt,
            }
        )

    rag_candidate = ScriptedModelProvider(
        scripts=rag_scripts,
        model="scripted-answer-v1",
        provider_id="scripted-answer-model",
    )
    no_rag_candidate = ScriptedModelProvider(
        scripts=no_rag_scripts,
        model="scripted-answer-v1",
        provider_id="scripted-answer-model",
    )

    candidate_pairs: list[dict[str, object]] = []
    judge_scripts: dict[str, str] = {}
    for index, prepared in enumerate(prepared_cases):
        question_id = str(prepared["questionId"])
        fixture = fixtures_by_id[question_id]
        rag_response = rag_candidate.generate(
            str(prepared["ragPrompt"]),
            response_id=f"rag:{question_id}",
            metadata={"variant": "rag", "question_id": question_id},
        )
        no_rag_response = no_rag_candidate.generate(
            str(prepared["noRagPrompt"]),
            response_id=f"no-rag:{question_id}",
            metadata={"variant": "no_rag", "question_id": question_id},
        )
        blind_order = (
            {"A": "rag", "B": "no_rag"}
            if index % 2 == 0
            else {"A": "no_rag", "B": "rag"}
        )
        answers_by_variant = {
            "rag": rag_response.text,
            "no_rag": no_rag_response.text,
        }
        scores_by_variant = {
            "rag": "1",
            "no_rag": str(fixture["noRagScore"]),
        }
        judge_prompt = (
            "Score candidate A and candidate B independently from 0 to 1 against "
            "the reference. Return JSON only.\n"
            f"Question: {prepared['question']}\n"
            f"Reference: {prepared['referenceAnswer']}\n"
            f"Candidate A: {answers_by_variant[blind_order['A']]}\n"
            f"Candidate B: {answers_by_variant[blind_order['B']]}"
        )
        blind_scores: dict[str, dict[str, str]] = {}
        for label, variant in blind_order.items():
            score = scores_by_variant[variant]
            if score == "1":
                rationale = "The answer matches the reference fact."
            elif score == "0.6":
                rationale = "The answer identifies the likely protocol but remains incomplete."
            else:
                rationale = "The answer contradicts the reference fact."
            blind_scores[label] = {
                "rationale": rationale,
                "score": score,
            }
        judge_scripts[judge_prompt] = json.dumps(
            {"scores": blind_scores},
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_pairs.append(
            {
                "answers": answers_by_variant,
                "blindOrder": blind_order,
                "judgePrompt": judge_prompt,
                "questionId": question_id,
                "responses": {
                    "rag": rag_response,
                    "no_rag": no_rag_response,
                },
            }
        )

    judge = ScriptedModelProvider(
        scripts=judge_scripts,
        model="scripted-interviewer-v1",
        provider_id="scripted-interviewer",
    )
    scored_by_question: dict[str, dict[str, dict[str, object]]] = {}
    blind_order_by_question: dict[str, dict[str, str]] = {}
    total_usage = {
        "input_characters": plan_response.usage["input_characters"],
        "output_characters": plan_response.usage["output_characters"],
    }
    for pair in candidate_pairs:
        question_id = str(pair["questionId"])
        judge_response = judge.generate(
            str(pair["judgePrompt"]),
            response_id=f"judge:{question_id}",
            metadata={
                "question_id": question_id,
                "role": "interviewer",
                "stage": "blind_pair_scoring",
            },
        )
        judgment = json.loads(judge_response.text)
        if not isinstance(judgment, dict) or not isinstance(judgment.get("scores"), dict):
            raise ValueError("interviewer judgment must contain blind A/B scores")
        blind_order = pair["blindOrder"]
        responses = pair["responses"]
        answers = pair["answers"]
        if not isinstance(blind_order, dict) or not isinstance(responses, dict):
            raise TypeError("candidate pair must contain responses and blind order")
        if not isinstance(answers, dict):
            raise TypeError("candidate pair must contain answers")
        blind_order_by_question[question_id] = dict(blind_order)
        scored_by_question[question_id] = {}
        for label in ("A", "B"):
            raw_score = judgment["scores"].get(label)
            if not isinstance(raw_score, dict) or not isinstance(raw_score.get("score"), str):
                raise ValueError("interviewer judgment labels must contain string scores")
            score = Decimal(raw_score["score"])
            if score < 0 or score > 1:
                raise ValueError("interviewer score must be between 0 and 1")
            variant = blind_order[label]
            response = responses[variant]
            scored_by_question[question_id][variant] = {
                "answer": str(answers[variant]),
                "judgeResponseId": judge_response.response_id,
                "rationale": str(raw_score.get("rationale", "")),
                "responseId": response.response_id,
                "score": score,
            }
            total_usage["input_characters"] += response.usage["input_characters"]
            total_usage["output_characters"] += response.usage["output_characters"]
        total_usage["input_characters"] += judge_response.usage["input_characters"]
        total_usage["output_characters"] += judge_response.usage["output_characters"]

    case_reports: list[dict[str, object]] = []
    rag_scores: list[Decimal] = []
    no_rag_scores: list[Decimal] = []
    rag_wins = 0
    for prepared in prepared_cases:
        question_id = str(prepared["questionId"])
        scored = scored_by_question[question_id]
        rag_result = scored["rag"]
        no_rag_result = scored["no_rag"]
        rag_score = rag_result["score"]
        no_rag_score = no_rag_result["score"]
        if not isinstance(rag_score, Decimal) or not isinstance(no_rag_score, Decimal):
            raise TypeError("interviewer scores must be Decimal values")
        rag_scores.append(rag_score)
        no_rag_scores.append(no_rag_score)
        winner = "tie"
        if rag_score > no_rag_score:
            winner = "rag"
            rag_wins += 1
        elif no_rag_score > rag_score:
            winner = "no_rag"
        retrieval = prepared["retrieval"]
        case_reports.append(
            {
                "questionId": question_id,
                "question": prepared["question"],
                "referenceAnswer": prepared["referenceAnswer"],
                "retrievedItemIds": [hit.item.item_id for hit in retrieval.hits],
                "blindOrder": blind_order_by_question[question_id],
                "rag": {**rag_result, "score": str(rag_score)},
                "noRag": {**no_rag_result, "score": str(no_rag_score)},
                "winner": winner,
            }
        )

    case_count = Decimal(len(case_reports))
    rag_mean = sum(rag_scores, Decimal(0)) / case_count
    no_rag_mean = sum(no_rag_scores, Decimal(0)) / case_count
    score_delta = rag_mean - no_rag_mean
    rag_win_rate = Decimal(rag_wins) / case_count
    baseline = ResourceSnapshotRef(
        resource_id="no-rag-candidate",
        digest=canonical_hash([item["noRag"] for item in case_reports]),
        resource_kind="benchmark_candidate",
    )
    candidate = ResourceSnapshotRef(
        resource_id="rag-candidate",
        digest=canonical_hash([item["rag"] for item in case_reports]),
        resource_kind="benchmark_candidate",
    )
    evaluator = {
        "case_count": len(case_reports),
        "kind": "llm-as-an-interviewer",
        "model": judge.model,
        "provider": judge.provider_id,
        "question_set_digest": canonical_hash(plan_contract),
    }
    metrics = [
        MetricObservation(
            "rag_mean_interview_score",
            rag_mean,
            direction="maximize",
            baseline_value=no_rag_mean,
            subject=candidate,
            evaluator=evaluator,
        ),
        MetricObservation(
            "no_rag_mean_interview_score",
            no_rag_mean,
            direction="maximize",
            subject=baseline,
            evaluator=evaluator,
        ),
        MetricObservation(
            "rag_score_delta",
            score_delta,
            direction="maximize",
            subject=candidate,
            evaluator=evaluator,
        ),
        MetricObservation(
            "rag_win_rate",
            rag_win_rate,
            direction="maximize",
            subject=candidate,
            evaluator=evaluator,
        ),
    ]
    gate = evaluate_gate(
        "rag-improves-interview-quality",
        candidate,
        metrics=metrics,
        constraints=[
            GateConstraint("rag_score_delta", "at_least", Decimal("0.20")),
            GateConstraint("rag_win_rate", "at_least", Decimal("0.67")),
        ],
    )
    trial = TrialResult(
        trial_id="rag-vs-no-rag-interview",
        base=baseline,
        candidate=candidate,
        metrics=metrics,
        gate=gate,
        outcome="accepted" if gate.decision == "pass" else "rejected",
    )
    evidence: dict[str, object] = {
        "benchmarkId": trial.trial_id,
        "interviewer": {
            "model": judge.model,
            "provider": judge.provider_id,
            "questionCount": len(case_reports),
            "questionSetDigest": canonical_hash(plan_contract),
        },
        "cases": case_reports,
        "summary": {
            "caseCount": len(case_reports),
            "gateDecision": gate.decision,
            "noRagMeanScore": str(no_rag_mean),
            "outcome": trial.outcome,
            "ragMeanScore": str(rag_mean),
            "ragScoreDelta": str(score_delta),
            "ragWinRate": str(rag_win_rate),
        },
        "metrics": [
            {
                "baselineValue": (
                    str(metric.baseline_value)
                    if metric.baseline_value is not None
                    else None
                ),
                "direction": metric.direction,
                "name": metric.name,
                "value": str(metric.value),
            }
            for metric in metrics
        ],
        "usage": total_usage,
    }
    return {**evidence, "evidenceDigest": canonical_hash(evidence)}
