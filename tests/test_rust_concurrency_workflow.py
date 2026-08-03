from __future__ import annotations

from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).parents[1]


def test_checkpoint_recovery_loom_model_is_pinned_and_required() -> None:
    manifest = tomllib.loads(
        (ROOT / "crates/graphblocks-runtime-durable/Cargo.toml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["dev-dependencies"]["loom"] == "=0.7.2"

    model_path = (
        ROOT / "crates/graphblocks-runtime-durable/tests/checkpoint_recovery_loom.rs"
    )
    model = model_path.read_text(encoding="utf-8")
    assert model.count("loom::model") == 3
    for transition in (
        "claim_latest_compatible",
        "renew_claim",
        "complete_claim",
        "fencing_epoch",
        "RecoveryClaimMismatch",
        "ActiveRecoveryClaim",
    ):
        assert transition in model

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    rust_steps = {step["name"]: step for step in workflow["jobs"]["rust"]["steps"]}
    loom_step = rust_steps["Run checkpoint recovery Loom model"]
    assert loom_step["shell"] == "bash"
    command = loom_step["run"]
    assert "set -o pipefail" in command
    assert "cargo test -p graphblocks-runtime-durable" in command
    assert "--test checkpoint_recovery_loom" in command
    assert "--locked" in command
    assert "2>&1 | tee dist/ci/rust-checkpoint-loom.log" in command


def test_loom_regression_is_in_the_workspace_lockfile() -> None:
    lock = tomllib.loads((ROOT / "Cargo.lock").read_text(encoding="utf-8"))
    packages = {(package["name"], package["version"]) for package in lock["package"]}
    assert ("loom", "0.7.2") in packages
