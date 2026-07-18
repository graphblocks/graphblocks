from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import graphblocks
import tools.check_compatibility as compatibility_module
from tools.check_compatibility import (
    _check_or_update,
    _dataclass_contract,
    build_testing_snapshot,
)


ROOT = Path(__file__).parents[1]
COMPATIBILITY_ROOT = ROOT / "compatibility"


def test_stable_compatibility_snapshots_match_the_implementation() -> None:
    completed = subprocess.run(
        [sys.executable, "tools/check_compatibility.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_snapshot_writer_requires_explicit_update_for_drift(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("{}\n", encoding="utf-8")
    contract = {"snapshotVersion": 1, "values": ["stable"]}

    assert _check_or_update(snapshot_path, contract, update=False) is False
    assert "compatibility snapshot drifted" in capsys.readouterr().err

    def newline_translating_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        del errors, newline
        return path.write_bytes(data.replace("\n", "\r\n").encode(encoding or "utf-8"))

    monkeypatch.setattr(Path, "write_text", newline_translating_write_text)
    assert _check_or_update(snapshot_path, contract, update=True) is True
    assert snapshot_path.read_bytes() == compatibility_module._render(contract).encode(
        "utf-8"
    )
    capsys.readouterr()
    assert _check_or_update(snapshot_path, contract, update=False) is True


def test_compatibility_cli_reports_missing_snapshot_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        compatibility_module,
        "PYTHON_SNAPSHOT_PATH",
        tmp_path / "missing-snapshot.json",
    )

    assert compatibility_module.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "compatibility snapshot error:" in captured.err
    assert "missing-snapshot.json" in captured.err
    assert "Traceback" not in captured.err


def test_nested_stable_paths_use_terminal_type_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class DeepType:
        pass

    def accepts_deep_type(value: DeepType) -> DeepType:
        return value

    accepts_deep_type.__annotations__ = {
        "value": "graphblocks.nested_api.DeepType",
        "return": "graphblocks.nested_api.DeepType",
    }

    class NestedApi:
        pass

    nested_api = NestedApi()
    nested_api.DeepType = DeepType
    nested_api.accepts_deep_type = accepts_deep_type
    monkeypatch.setattr(graphblocks, "nested_api", nested_api, raising=False)
    policy_path = tmp_path / "nested-surface.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "snapshotVersion": 1,
                "targetRelease": "1.0.0",
                "readiness": "candidate",
                "symbols": [
                    {
                        "path": "graphblocks.nested_api.accepts_deep_type",
                        "profile": "GB-C1-LOCAL-RUNTIME",
                    }
                ],
                "referencedTypes": [
                    {
                        "path": "graphblocks.nested_api.DeepType",
                        "kind": "opaque-type",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    snapshot = compatibility_module._build_python_snapshot(
        policy_path,
        package="graphblocks",
    )

    assert snapshot["symbols"][0]["typeReferences"] == ["DeepType"]


def test_nested_stable_type_names_must_be_unambiguous(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class NestedApi:
        pass

    left = NestedApi()
    right = NestedApi()
    left.Duplicate = type("Duplicate", (), {})
    right.Duplicate = type("Duplicate", (), {})
    monkeypatch.setattr(graphblocks, "left_api", left, raising=False)
    monkeypatch.setattr(graphblocks, "right_api", right, raising=False)
    policy_path = tmp_path / "ambiguous-nested-surface.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "snapshotVersion": 1,
                "targetRelease": "1.0.0",
                "readiness": "candidate",
                "symbols": [
                    {
                        "path": "graphblocks.left_api.Duplicate",
                        "profile": "GB-C1-LOCAL-RUNTIME",
                    },
                    {
                        "path": "graphblocks.right_api.Duplicate",
                        "profile": "GB-C1-LOCAL-RUNTIME",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate stable type name 'Duplicate'"):
        compatibility_module._build_python_snapshot(
            policy_path,
            package="graphblocks",
        )


def test_stable_callable_names_do_not_count_as_public_types(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def run(value: object) -> object:
        return value

    def accepts_fake_type(value: object) -> object:
        return value

    accepts_fake_type.__annotations__ = {"value": "run", "return": "run"}

    class NestedApi:
        pass

    nested_api = NestedApi()
    nested_api.accepts_fake_type = accepts_fake_type
    monkeypatch.setattr(graphblocks, "run", run, raising=False)
    monkeypatch.setattr(graphblocks, "nested_api", nested_api, raising=False)
    policy_path = tmp_path / "method-name-surface.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "snapshotVersion": 1,
                "targetRelease": "1.0.0",
                "readiness": "candidate",
                "symbols": [
                    {
                        "path": "graphblocks.run",
                        "profile": "GB-C1-LOCAL-RUNTIME",
                    },
                    {
                        "path": "graphblocks.nested_api.accepts_fake_type",
                        "profile": "GB-C1-LOCAL-RUNTIME",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unlisted public type.*run"):
        compatibility_module._build_python_snapshot(
            policy_path,
            package="graphblocks",
        )


def test_dataclass_snapshot_captures_behavioral_contracts() -> None:
    class ContractBase:
        __slots__ = ()

    @dataclass(
        eq=True,
        order=True,
        unsafe_hash=True,
        repr=False,
        slots=True,
        weakref_slot=True,
        match_args=False,
    )
    class Contract(ContractBase):
        value: str = field(
            default="value",
            compare=False,
            hash=True,
            repr=False,
            kw_only=True,
        )

    contract = _dataclass_contract(Contract)

    assert contract is not None
    assert contract["bases"] == [
        f"{ContractBase.__module__}.{ContractBase.__qualname__}"
    ]
    assert {
        name: contract[name]
        for name in (
            "eq",
            "frozen",
            "init",
            "matchArgs",
            "order",
            "repr",
            "slots",
            "unsafeHash",
            "weakrefSlot",
        )
    } == {
        "eq": True,
        "frozen": False,
        "init": True,
        "matchArgs": None,
        "order": True,
        "repr": False,
        "slots": True,
        "unsafeHash": True,
        "weakrefSlot": True,
    }
    assert contract["fields"] == [
        {
            "name": "value",
            "annotation": "str",
            "default": {"kind": "value", "value": "value"},
            "init": True,
            "keywordOnly": True,
            "compare": False,
            "hash": True,
            "repr": False,
        }
    ]


def test_dataclass_snapshot_detects_equality_policy_drift() -> None:
    @dataclass(eq=True)
    class Comparable:
        value: str

    @dataclass(eq=False)
    class IdentityOnly:
        value: str

    assert _dataclass_contract(Comparable) != _dataclass_contract(IdentityOnly)


def test_stable_python_surface_is_deliberate_and_profile_bounded() -> None:
    policy = yaml.safe_load(
        (COMPATIBILITY_ROOT / "stable-python-surface.yaml").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-python-api.json").read_text(encoding="utf-8")
    )

    assert policy["readiness"] == snapshot["readiness"] == "candidate"
    assert {entry["profile"] for entry in policy["symbols"]} == {
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    }
    assert [entry["path"] for entry in policy["symbols"]] == [
        entry["path"] for entry in snapshot["symbols"]
    ]
    assert len(snapshot["symbols"]) < 55
    assert all(entry["path"].startswith("graphblocks.") for entry in snapshot["symbols"])
    assert all(
        "signature" in entry or entry["kind"] == "type-alias"
        for entry in snapshot["symbols"]
    )
    stable_type_names = {
        entry["path"].split(".", 2)[1] for entry in snapshot["symbols"]
    }
    assert stable_type_names <= set(graphblocks.__all__)
    assert all(
        set(entry.get("typeReferences", ())) <= stable_type_names
        for entry in snapshot["symbols"]
    )
    for preview_type in (
        "InProcessRuntime",
        "RunDeploymentProvenance",
        "RuntimeCheckpoint",
        "RunResult",
    ):
        assert preview_type not in stable_type_names
        assert all(
            preview_type not in entry.get("typeReferences", ())
            for entry in snapshot["symbols"]
        )
    assert "waiting_callback" not in json.dumps(snapshot)


def test_stable_cli_snapshot_covers_success_failure_json_and_exit_codes() -> None:
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-cli-contracts.json").read_text(encoding="utf-8")
    )

    assert snapshot["readiness"] == "candidate"
    assert snapshot["stdoutContract"] == "parsed-json"
    assert {case["command"] for case in snapshot["cases"]} == {
        "validate",
        "plan",
        "run",
    }
    for command in ("validate", "plan", "run"):
        command_cases = [case for case in snapshot["cases"] if case["command"] == command]
        assert {case["exitCode"] for case in command_cases} == {0, 1}
        assert all(isinstance(case["stdoutJson"], dict) for case in command_cases)
        assert all(case["stderr"] == "" for case in command_cases)
    plan_expand = next(
        case for case in snapshot["cases"] if case["id"] == "plan-expand-success"
    )
    assert plan_expand["argv"][-1] == "--expand"
    assert plan_expand["stdoutJson"]["graph"]["apiVersion"] == "graphblocks.ai/v1"
    unknown_opt_in_cases = {
        case["id"]: case
        for case in snapshot["cases"]
        if case["id"] in {
            "validate-unknown-block-opt-in",
            "plan-unknown-block-opt-in",
        }
    }
    assert set(unknown_opt_in_cases) == {
        "validate-unknown-block-opt-in",
        "plan-unknown-block-opt-in",
    }
    assert all(
        "--allow-unknown-blocks" in case["argv"]
        and case["exitCode"] == 0
        and case["stdoutJson"]["ok"] is True
        for case in unknown_opt_in_cases.values()
    )


def test_stable_testing_surface_is_deliberate_and_profile_bounded() -> None:
    policy = yaml.safe_load(
        (COMPATIBILITY_ROOT / "stable-testing-surface.yaml").read_text(encoding="utf-8")
    )
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-testing-api.json").read_text(encoding="utf-8")
    )

    assert policy["readiness"] == snapshot["readiness"] == "candidate"
    assert {entry["profile"] for entry in policy["symbols"]} == {
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    }
    assert [entry["path"] for entry in policy["symbols"]] == [
        entry["path"] for entry in snapshot["symbols"]
    ]
    assert all(
        entry["path"].startswith("graphblocks_testing.")
        for entry in snapshot["symbols"]
    )
    referenced_types = {
        entry["path"]: entry for entry in snapshot["referencedTypes"]
    }
    assert referenced_types["graphblocks_testing.TckCaseKind"]["kind"] == "type-alias"
    assert referenced_types["graphblocks_testing.TckResultStatus"]["value"] == (
        "typing.Literal['passed', 'failed']"
    )
    assert referenced_types["graphblocks_testing.TckCase"] == {
        "kind": "opaque-type",
        "module": "graphblocks_testing",
        "path": "graphblocks_testing.TckCase",
        "qualname": "TckCase",
    }
    result_symbol = next(
        entry
        for entry in snapshot["symbols"]
        if entry["path"] == "graphblocks_testing.TckResult"
    )
    assert result_symbol["typeReferences"] == ["TckCaseKind", "TckResultStatus"]


def test_stable_testing_snapshot_binds_referenced_type_alias_values(monkeypatch) -> None:
    expected = build_testing_snapshot()
    import graphblocks_testing

    monkeypatch.setattr(graphblocks_testing, "TckResultStatus", object())

    assert build_testing_snapshot() != expected


def test_stable_testing_cli_snapshot_covers_list_and_run_all_contracts() -> None:
    snapshot = json.loads(
        (COMPATIBILITY_ROOT / "stable-testing-cli-contracts.json").read_text(
            encoding="utf-8"
        )
    )

    assert snapshot["readiness"] == "candidate"
    assert snapshot["stdoutContract"] == "parsed-json"
    assert {case["command"] for case in snapshot["cases"]} == {"list", "run-all"}
    assert {case["exitCode"] for case in snapshot["cases"]} == {0, 1}
    assert all(isinstance(case["stdoutJson"], dict) for case in snapshot["cases"])
    assert all(case["stderr"] == "" for case in snapshot["cases"])
