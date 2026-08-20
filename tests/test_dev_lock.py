from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools import check_dev_lock


ROOT = Path(__file__).parents[1]
LOCK_PATH = ROOT / "requirements" / "dev.lock"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def test_development_lock_is_exact_portable_and_covers_test_dependencies() -> None:
    names = check_dev_lock.validate_lock(LOCK_PATH.read_text(encoding="utf-8"))

    assert len(names) >= 20
    assert {
        "diff-cover",
        "hypothesis",
        "jsonschema",
        "mypy",
        "pytest",
        "pytest-cov",
        "pyyaml",
        "ruff",
    } <= names


def test_development_lock_generation_uses_a_checkout_relative_source(
    tmp_path: Path,
) -> None:
    command = check_dev_lock.compile_command(
        tmp_path / "dev.lock", constrain_existing=True
    )

    assert command[-1] == "pyproject.toml"
    assert command[command.index("--constraint") + 1] == "requirements/dev.lock"
    assert str(check_dev_lock.ROOT) not in command

    unconstrained = check_dev_lock.compile_command(
        tmp_path / "dev.lock", constrain_existing=False
    )
    assert "--constraint" not in unconstrained


def test_development_lock_rejects_platform_markers_and_non_exact_pins() -> None:
    header = check_dev_lock.LOCK_HEADER
    with pytest.raises(check_dev_lock.DevLockError, match="exact version pin"):
        check_dev_lock.validate_lock(header + "pytest>=9\n")
    with pytest.raises(check_dev_lock.DevLockError, match="platform marker"):
        check_dev_lock.validate_lock(
            header + 'pytest==9.1.1 ; sys_platform == "linux"\n'
        )


def test_development_lock_check_detects_regeneration_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = LOCK_PATH.read_text(encoding="utf-8")
    generated_body = committed.removeprefix(check_dev_lock.LOCK_HEADER)
    compile_modes: list[bool] = []

    def compile_unchanged(*, constrain_existing: bool) -> str:
        compile_modes.append(constrain_existing)
        return generated_body

    monkeypatch.setattr(check_dev_lock, "_compile_lock_body", compile_unchanged)
    assert check_dev_lock.check_dev_lock() == 0
    assert compile_modes == [True]

    monkeypatch.setattr(
        check_dev_lock,
        "_compile_lock_body",
        lambda *, constrain_existing: generated_body.replace(
            "pytest==9.1.1", "pytest==9.1.0"
        ),
    )
    with pytest.raises(check_dev_lock.DevLockError, match="pytest==9.1.0"):
        check_dev_lock.check_dev_lock()


def test_development_lock_write_resolves_without_the_committed_constraint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_path = tmp_path / "dev.lock"
    generated_body = LOCK_PATH.read_text(encoding="utf-8").removeprefix(
        check_dev_lock.LOCK_HEADER
    )
    compile_modes: list[bool] = []

    def compile_fresh(*, constrain_existing: bool) -> str:
        compile_modes.append(constrain_existing)
        return generated_body

    monkeypatch.setattr(check_dev_lock, "ROOT", tmp_path)
    monkeypatch.setattr(check_dev_lock, "LOCK_PATH", output_path)
    monkeypatch.setattr(check_dev_lock, "_compile_lock_body", compile_fresh)

    assert check_dev_lock.check_dev_lock(write=True) == 0
    assert compile_modes == [False]
    assert output_path.read_text(encoding="utf-8").startswith(
        check_dev_lock.LOCK_HEADER
    )


def test_ci_regenerates_and_installs_from_the_development_lock() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    quick_steps = {step["name"]: step for step in jobs["python-quality"]["steps"]}

    install = quick_steps["Install quick-gate dependencies"]["run"]
    assert "python -m pip install pip-tools==7.6.0" in install
    assert "python tools/check_dev_lock.py" in install
    assert 'python -m pip install -c requirements/dev.lock -e ".[test]"' in install
    for job_name in ("python-quality", "python", "installed-artifacts", "examples"):
        cache_paths = jobs[job_name]["steps"][
            next(
                index
                for index, step in enumerate(jobs[job_name]["steps"])
                if step["name"] == "Set up Python"
            )
        ]["with"]["cache-dependency-path"]
        assert cache_paths == "pyproject.toml\nrequirements/dev.lock\n"

    for job_name, step_name in (
        ("python", "Install test dependencies"),
        ("installed-artifacts", "Install wheel verification tooling"),
        ("examples", "Install test dependencies"),
    ):
        steps = {step["name"]: step for step in jobs[job_name]["steps"]}
        assert (
            'pip install -c requirements/dev.lock -e ".[test]"'
            in steps[step_name]["run"]
        )
