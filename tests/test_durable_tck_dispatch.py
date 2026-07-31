from __future__ import annotations

import ast
from collections import Counter
from dataclasses import fields, is_dataclass
import inspect
import json
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import get_args

import pytest

from graphblocks_testing import (
    TckCase,
    TckRunner,
    load_durable_tck_cases,
    stdlib_registry,
)
from graphblocks_testing import durable_cases as durable_case_module
from graphblocks_testing import fixture_loading as fixture_loading_module
from graphblocks_testing import runners as runner_module
from graphblocks_testing.durable_cases import (
    DURABLE_CASE_DECODERS,
    DURABLE_CASE_HANDLERS,
    DurableCaseContext,
    DurableCaseEnvelope,
    run_durable_case,
)
from graphblocks_testing.durable_contracts import (
    DURABLE_CASE_KINDS,
    DurableCaseKind,
)


ROOT = Path(__file__).parents[1]
EXPECTED_DURABLE_REPORT_DIGEST = (
    "sha256:3ed1530d8755a7a27e9ed2876bcbe8a258a35ba996828857ba201b3af5d21ddc"
)


def test_durable_fixture_kinds_have_exactly_one_handler() -> None:
    raw_cases = json.loads(
        (ROOT / "tck" / "durable" / "cases.json").read_text(encoding="utf-8")
    )
    fixture_counts = Counter(case["kind"] for case in raw_cases)

    assert len(raw_cases) == 331
    assert set(fixture_counts) == DURABLE_CASE_KINDS
    assert set(get_args(DurableCaseKind)) == DURABLE_CASE_KINDS
    assert set(DURABLE_CASE_DECODERS) == DURABLE_CASE_KINDS
    assert set(DURABLE_CASE_HANDLERS) == DURABLE_CASE_KINDS
    assert len(set(DURABLE_CASE_DECODERS.values())) == len(DURABLE_CASE_DECODERS)
    assert len(set(DURABLE_CASE_HANDLERS.values())) == len(DURABLE_CASE_HANDLERS)
    assert all(is_dataclass(decoder) for decoder in DURABLE_CASE_DECODERS.values())
    assert all(
        decoder.__dataclass_params__.frozen
        for decoder in DURABLE_CASE_DECODERS.values()
    )
    common_fields = {field.name for field in fields(DurableCaseContext)}
    assert all(
        {field.name for field in fields(decoder)} - common_fields
        for decoder in DURABLE_CASE_DECODERS.values()
    )
    assert all(
        "_decode_fixture" in decoder.__dict__
        for decoder in DURABLE_CASE_DECODERS.values()
    )
    assert all(
        inspect.get_annotations(handler, eval_str=True)["context"] is decoder
        for kind, handler in DURABLE_CASE_HANDLERS.items()
        for decoder in (DURABLE_CASE_DECODERS[kind],)
    )
    assert all(callable(handler) for handler in DURABLE_CASE_HANDLERS.values())
    assert all(fixture_counts[kind] > 0 for kind in DURABLE_CASE_KINDS)
    with pytest.raises(TypeError):
        DURABLE_CASE_HANDLERS["unknown"] = run_durable_case  # type: ignore[index]
    with pytest.raises(TypeError):
        DURABLE_CASE_DECODERS["unknown"] = object  # type: ignore[index,assignment]


def test_durable_kind_decoders_are_exact_and_fail_closed() -> None:
    durable = ModuleType("test_durable")
    for kind, decoder in DURABLE_CASE_DECODERS.items():
        envelope = DurableCaseEnvelope(
            kind=kind,
            fixture={"kind": kind},
            expected={},
            expected_diagnostics=None,
        )

        context = decoder.decode(
            envelope,
            durable=durable,
            diagnostics=[],
            expected_keys_with_structural_diagnostics=set(),
        )

        assert type(context) is decoder
        assert context.kind == kind

        wrong_envelope = DurableCaseEnvelope(
            kind="not-" + kind,
            fixture={},
            expected={},
            expected_diagnostics=None,
        )
        with pytest.raises(ValueError, match="received"):
            decoder.decode(
                wrong_envelope,
                durable=durable,
                diagnostics=[],
                expected_keys_with_structural_diagnostics=set(),
            )


def test_durable_fixture_loading_depends_only_on_kind_contract() -> None:
    tree = ast.parse(inspect.getsource(fixture_loading_module))
    relative_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1
    }

    assert "durable_contracts" in relative_imports
    assert "durable_cases" not in relative_imports


def test_durable_loader_rejects_unknown_kind(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "unknown-kind",
                    "kind": "unknown",
                    "expected": {},
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="durable TCK case unknown-kind has unsupported kind 'unknown'",
    ):
        load_durable_tck_cases(path)


@pytest.mark.parametrize("whitelist_diagnostic", [False, True])
def test_durable_runner_fails_closed_for_unknown_kind(
    whitelist_diagnostic: bool,
) -> None:
    diagnostic = {
        "code": "DurableKindUnknown",
        "message": "durable TCK kind 'unknown' is not supported",
        "path": "$.kind",
    }
    fixture: dict[str, object] = {
        "kind": "unknown",
        "expected": {},
    }
    if whitelist_diagnostic:
        fixture["expectedDiagnostics"] = [diagnostic]
    case = TckCase.durable(
        case_id=f"unknown-kind-{whitelist_diagnostic}",
        fixture=fixture,
    )

    result = TckRunner(stdlib_registry()).run_cases((case,)).results[0]

    assert result.status == "failed"
    assert result.diagnostics == (diagnostic,)
    assert result.observed == {}


def test_known_durable_kind_missing_package_bypasses_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "No module named 'graphblocks.durable'"
    diagnostic = {
        "code": "DurablePackageMissing",
        "message": message,
        "path": "$",
    }

    def raise_missing_package(name: str) -> ModuleType:
        assert name == "graphblocks.durable"
        raise ModuleNotFoundError(message)

    monkeypatch.setattr(
        durable_case_module.importlib,
        "import_module",
        raise_missing_package,
    )
    case = TckCase.durable(
        case_id="known-kind-missing-package",
        fixture={
            "kind": "source_replay",
            "expected": {},
            "expectedDiagnostics": [diagnostic],
        },
    )

    result = run_durable_case(case)

    assert result.status == "failed"
    assert result.diagnostics == (diagnostic,)
    assert result.observed == {}


def test_known_durable_handler_error_preserves_expected_reconciliation() -> None:
    diagnostic = {
        "code": "DurableExecutionError",
        "message": "durable source_replay case requires events",
        "path": "$",
    }
    case = TckCase.durable(
        case_id="known-kind-handler-error",
        fixture={
            "kind": "source_replay",
            "events": {},
            "expected": {},
            "expectedDiagnostics": [diagnostic],
        },
    )

    result = run_durable_case(case)

    assert result.status == "passed"
    assert result.diagnostics == ()
    assert result.observed == {"expectedDiagnosticsMatched": True}


def test_durable_dispatch_boundaries_are_bounded() -> None:
    method_source = dedent(inspect.getsource(runner_module.TckRunner._run_durable_case))
    method = next(
        node
        for node in ast.parse(method_source).body
        if isinstance(node, ast.FunctionDef)
    )
    dispatcher_source = inspect.getsource(run_durable_case)
    dispatcher = next(
        node
        for node in ast.parse(dispatcher_source).body
        if isinstance(node, ast.FunctionDef)
    )
    handler_sources = {
        kind: inspect.getsource(handler)
        for kind, handler in DURABLE_CASE_HANDLERS.items()
    }

    assert method.end_lineno is not None
    assert method.end_lineno - method.lineno + 1 <= 5
    assert not any(isinstance(node, ast.If) for node in ast.walk(method))
    assert dispatcher.end_lineno is not None
    assert dispatcher.end_lineno - dispatcher.lineno + 1 <= 120
    assert len(handler_sources) == 15
    assert max(len(source.splitlines()) for source in handler_sources.values()) <= 1_600
    assert all(
        not any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "context"
            and node.attr == "fixture"
            for node in ast.walk(ast.parse(source))
        )
        for source in handler_sources.values()
    )


def test_durable_dispatch_preserves_full_report_contract_digest() -> None:
    cases = load_durable_tck_cases(ROOT / "tck" / "durable" / "cases.json")
    report = TckRunner(
        stdlib_registry(),
        suite="durable",
        fixture_digest="sha256:" + ("0" * 64),
        implementation="graphblocks-python",
        implementation_version="test",
    ).run_cases(cases)

    assert report.ok
    assert len(report.results) == 331
    assert report.content_digest() == EXPECTED_DURABLE_REPORT_DIGEST
