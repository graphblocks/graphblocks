from __future__ import annotations

from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).parents[1]
FUZZ_TOOLCHAIN = "nightly-2026-04-22"
CARGO_FUZZ_VERSION = "0.13.2"
LIBFUZZER_SYS_VERSION = "=0.4.13"


def test_canonical_fuzz_target_is_version_locked_and_seeded() -> None:
    manifest = tomllib.loads(
        (ROOT / "fuzz" / "Cargo.toml").read_text(encoding="utf-8")
    )

    assert manifest["package"]["publish"] is False
    assert manifest["package"]["metadata"]["cargo-fuzz"] is True
    assert manifest["dependencies"]["libfuzzer-sys"] == LIBFUZZER_SYS_VERSION
    assert manifest["dependencies"]["graphblocks-schema"] == {
        "path": "../crates/graphblocks-schema"
    }
    assert manifest["bin"] == [
        {
            "name": "canonical_json",
            "path": "fuzz_targets/canonical_json.rs",
            "test": False,
            "doc": False,
            "bench": False,
        }
    ]
    lock = tomllib.loads((ROOT / "fuzz" / "Cargo.lock").read_text(encoding="utf-8"))
    locked_packages = {
        (package["name"], package["version"]) for package in lock["package"]
    }
    assert ("graphblocks-security-fuzz", "0.0.0") in locked_packages
    assert ("libfuzzer-sys", "0.4.13") in locked_packages

    corpus_root = ROOT / "fuzz" / "corpus" / "canonical_json"
    expected_seeds = {
        "depth-boundary": b"D>\n",
        "duplicate-key": b"Kduplicate\n",
        "integer-boundary": b"I>!\n",
        "raw-valid": b'R{"a":[1,true,null],"b":"graphblocks"}\n',
        "schema-id": b"Sschemas/Message@1\n",
    }
    assert {
        path.name: path.read_bytes() for path in corpus_root.iterdir()
    } == expected_seeds
    assert all(
        path.is_file() and 0 < path.stat().st_size <= 1_024
        for path in corpus_root.iterdir()
    )

    target_source = (
        ROOT / "fuzz" / "fuzz_targets" / "canonical_json.rs"
    ).read_text(encoding="utf-8")
    for required_oracle in (
        "CanonicalJsonParseError::DuplicateObjectKey",
        "CanonicalJsonError::NestingTooDeep",
        "CanonicalJsonError::IntegerTooLarge",
    ):
        assert required_oracle in target_source


def test_security_fuzz_workflow_has_bounded_pr_and_scheduled_campaigns() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "security-fuzz.yml").read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["group"] == (
        "${{ github.workflow }}-${{ github.event_name }}-${{ github.ref }}"
    )
    assert workflow["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' || "
        "github.event_name == 'push' }}"
    )
    assert set(workflow["on"]) == {
        "pull_request",
        "push",
        "schedule",
        "workflow_dispatch",
    }
    assert workflow["on"]["schedule"] == [{"cron": "17 3 * * 1"}]

    job = workflow["jobs"]["canonical-security-fuzz"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "45"
    assert job["env"] == {
        "FUZZ_TOOLCHAIN": FUZZ_TOOLCHAIN,
        "CARGO_FUZZ_VERSION": CARGO_FUZZ_VERSION,
    }
    steps = {step["name"]: step for step in job["steps"]}

    assert steps["Check out repository"]["uses"] == (
        "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10"
    )
    toolchain_setup = steps["Install pinned fuzz toolchain"]["run"]
    assert 'rustup toolchain install "$FUZZ_TOOLCHAIN" --profile minimal' in toolchain_setup
    assert (
        'cargo +"$FUZZ_TOOLCHAIN" install cargo-fuzz'
        in toolchain_setup
    )
    assert '--version "$CARGO_FUZZ_VERSION"' in toolchain_setup
    assert "--locked" in toolchain_setup

    locked_preflight = steps["Validate locked fuzz dependency graph"]["run"]
    for required_argument in (
        "--manifest-path fuzz/Cargo.toml",
        "--locked",
        "--format-version 1",
        "dist/fuzz/cargo-metadata.json",
    ):
        assert required_argument in locked_preflight

    pr_smoke = steps["Run bounded seed-corpus mutation smoke"]
    assert pr_smoke["if"] == (
        "github.event_name != 'schedule' && "
        "github.event_name != 'workflow_dispatch'"
    )
    for required_argument in (
        "fuzz/corpus/canonical_json",
        "-runs=10000",
        "-max_len=16384",
        "-timeout=10",
        "-rss_limit_mb=2048",
    ):
        assert required_argument in pr_smoke["run"]

    scheduled = steps["Run scheduled bounded fuzz campaign"]
    assert scheduled["if"] == (
        "github.event_name == 'schedule' || "
        "github.event_name == 'workflow_dispatch'"
    )
    for required_argument in (
        "fuzz/corpus/canonical_json",
        "-max_total_time=1800",
        "-max_len=16384",
        "-timeout=10",
        "-rss_limit_mb=2048",
    ):
        assert required_argument in scheduled["run"]

    upload = steps["Retain fuzz corpus and crash artifacts"]
    lock_verification = steps["Verify fuzz lockfile stayed unchanged"]
    assert lock_verification["if"] == "always()"
    assert lock_verification["run"] == "git diff --exit-code -- fuzz/Cargo.lock"
    assert upload["if"] == "always()"
    assert upload["uses"] == (
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
    )
    assert "dist/fuzz/cargo-metadata.json" in upload["with"]["path"]


def test_fuzz_gate_remains_release_blocking_and_documented() -> None:
    matrix = yaml.safe_load(
        (ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text(
            encoding="utf-8"
        )
    )
    audit_gate = next(
        gate
        for gate in matrix["releaseGates"]
        if gate["id"] == "REL-AUDIT-REMEDIATION"
    )

    assert audit_gate["readiness"] == (
        "code-closure-enforced-external-evidence-blocked"
    )
    assert (
        "hypothesis-cargo-fuzz-and-proptest-pr-smoke-and-scheduled"
        in audit_gate["implementedEvidence"]
    )
    assert "docs/project/security-fuzzing.md" in audit_gate["evidence"]

    fuzzing_doc = (
        ROOT / "docs" / "project" / "security-fuzzing.md"
    ).read_text(encoding="utf-8")
    for required_text in (
        "GB-QA-008",
        "10,000",
        "30 minutes",
        "16,384 bytes",
        "2,048 MiB",
        "10 seconds",
        "nightly-2026-04-22",
        "cargo-fuzz 0.13.2",
        "REL-AUDIT-REMEDIATION",
    ):
        assert required_text in fuzzing_doc
