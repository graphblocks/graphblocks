from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import graphblocks._outcome_reference as outcome_reference


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tck" / "outcome" / "cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_outcome_reference_matches_exact_fixture_without_reading_expected(
    case: dict[str, object],
) -> None:
    execution_case = (
        deepcopy(case["request"])
        if "request" in case
        else {key: deepcopy(value) for key, value in case.items() if key != "expected"}
    )

    assert (
        outcome_reference.evaluate_outcome_tck_case_reference(execution_case)
        == case["expected"]
    )


def test_outcome_reference_execution_contract_excludes_expected() -> None:
    case = deepcopy(CASES[0])
    expected = case["expected"]
    assert isinstance(expected, dict)
    expected["outcome"] = {"status": "absent"}
    execution_case = {key: value for key, value in case.items() if key != "expected"}

    result = outcome_reference.evaluate_outcome_tck_case_reference(execution_case)

    assert result["outcome"] == CASES[0]["expected"]["outcome"]
    assert outcome_reference.evaluate_outcome_tck_case_reference(case) == {
        "contractVersion": "graphblocks.outcome.tck.v1",
        "ok": False,
        "scenario": "normalize_outcome",
        "errorCategory": "unknown_field",
    }


def test_outcome_reference_uses_public_outcome_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []
    original = outcome_reference.Outcome.value

    def tracking_value(cls: type[object], value: object) -> object:
        observed.append(value)
        return original(value)

    monkeypatch.setattr(
        outcome_reference.Outcome,
        "value",
        classmethod(tracking_value),
    )

    result = outcome_reference.evaluate_outcome_tck_case_reference(
        {
            "name": "public-facade",
            "scenario": "normalize_outcome",
            "outcome": {"status": "value", "value": {"ok": True}},
        }
    )

    assert result["ok"] is True
    assert observed == [{"ok": True}]


@pytest.mark.parametrize(
    ("outcome", "error_category"),
    (
        (
            {
                "status": "skipped",
                "reason": {"code": "condition_false", "message": " "},
            },
            "invalid_outcome",
        ),
        (
            {
                "status": "skipped",
                "reason": {"code": "condition_false", "message": "bad\nmessage"},
            },
            "invalid_outcome",
        ),
        (
            {
                "status": "failed",
                "error": {
                    "code": "provider.timeout",
                    "category": "timeout",
                    "message": "provider timed out",
                    "retryable": True,
                    "details": {},
                    "causeChain": [" "],
                },
            },
            "invalid_outcome",
        ),
        (
            {
                "status": "skipped",
                "reason": {"code": " condition_false ", "message": None},
            },
            "invalid_identifier",
        ),
        (
            {
                "status": "skipped",
                "reason": {"code": 1, "message": None},
            },
            "invalid_identifier",
        ),
    ),
)
def test_outcome_reference_rejects_noncanonical_text(
    outcome: dict[str, object],
    error_category: str,
) -> None:
    assert outcome_reference.evaluate_outcome_tck_case_reference(
        {
            "name": "invalid-text",
            "scenario": "normalize_outcome",
            "outcome": outcome,
        }
    ) == {
        "contractVersion": "graphblocks.outcome.tck.v1",
        "ok": False,
        "scenario": "normalize_outcome",
        "errorCategory": error_category,
    }


def test_outcome_reference_preserves_human_message_surrounding_whitespace() -> None:
    result = outcome_reference.evaluate_outcome_tck_case_reference(
        {
            "name": "message-whitespace",
            "scenario": "normalize_outcome",
            "outcome": {
                "status": "skipped",
                "reason": {
                    "code": "condition_false",
                    "message": " preserved message ",
                },
            },
        }
    )

    assert result["outcome"] == {
        "status": "skipped",
        "reason": {
            "code": "condition_false",
            "message": " preserved message ",
        },
    }


def test_outcome_reference_enforces_the_json_depth_boundary() -> None:
    nested: object = None
    for depth in range(1, 64):
        nested = [nested]
        result = outcome_reference.evaluate_outcome_tck_case_reference(
            {
                "name": "value-depth-boundary",
                "scenario": "normalize_outcome",
                "outcome": {"status": "value", "value": nested},
            }
        )
        assert result["ok"] is (depth <= 62)


@pytest.mark.parametrize(
    "value",
    ("\ud800", 10**10_000),
    ids=("surrogate", "over-limit-integer"),
)
def test_outcome_reference_closes_canonical_admission_failures(value: object) -> None:
    assert outcome_reference.evaluate_outcome_tck_case_reference(
        {
            "name": "canonical-admission-failure",
            "scenario": "normalize_outcome",
            "outcome": {"status": "value", "value": value},
        }
    ) == {
        "contractVersion": "graphblocks.outcome.tck.v1",
        "ok": False,
        "scenario": "normalize_outcome",
        "errorCategory": "invalid_outcome",
    }
