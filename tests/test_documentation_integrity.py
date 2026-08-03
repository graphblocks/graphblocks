from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
import re
import shlex
import tomllib

import pytest
import yaml


ROOT = Path(__file__).parents[1]


def _validate_real_service_evidence(
    repository_root: Path,
    integration_id: str,
    evidence: object,
    schema: dict[str, object],
) -> None:
    assert isinstance(evidence, dict)
    required_fields = schema["requiredFields"]
    assert isinstance(required_fields, list)
    assert set(evidence) == set(required_fields)

    resolved_root = repository_root.resolve()
    resolved_paths: dict[str, Path] = {}
    for field, prefix_field in (
        ("test", "testPathPrefix"),
        ("workflow", "workflowPathPrefix"),
    ):
        value = evidence[field]
        prefix = schema[prefix_field]
        assert isinstance(value, str) and value
        assert isinstance(prefix, str) and prefix
        relative_path = PurePosixPath(value)
        assert not relative_path.is_absolute()
        assert "\\" not in value
        assert ".." not in relative_path.parts
        assert relative_path.as_posix() == value
        assert value.startswith(prefix)
        resolved_path = (repository_root / relative_path).resolve()
        assert resolved_path.is_relative_to(resolved_root)
        assert resolved_path.is_file()
        resolved_paths[field] = resolved_path

    artifact_prefix = schema["artifactPathPrefix"]
    assert isinstance(artifact_prefix, str) and artifact_prefix
    generated_paths: dict[str, str] = {}
    for field in ("resultPath", "reportPath", "signaturePath"):
        value = evidence[field]
        assert isinstance(value, str) and value
        relative_path = PurePosixPath(value)
        assert not relative_path.is_absolute()
        assert "\\" not in value
        assert ".." not in relative_path.parts
        assert relative_path.as_posix() == value
        assert value.startswith(artifact_prefix)
        assert (repository_root / relative_path).resolve().is_relative_to(
            resolved_root
        )
        generated_paths[field] = value
    assert len(set(generated_paths.values())) == len(generated_paths)
    assert generated_paths["resultPath"].endswith(".json")
    assert generated_paths["reportPath"].endswith(".json")
    assert generated_paths["signaturePath"].endswith(".sigstore.json")

    artifact_name = evidence["artifactName"]
    artifact_name_bindings = schema["artifactNameBindings"]
    assert isinstance(artifact_name, str) and integration_id in artifact_name
    assert isinstance(artifact_name_bindings, list)
    assert all(
        isinstance(binding, str) and binding in artifact_name
        for binding in artifact_name_bindings
    )

    workflow = yaml.safe_load(resolved_paths["workflow"].read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    workflow_job = evidence["workflowJob"]
    assert isinstance(workflow_job, str) and workflow_job
    job = jobs.get(workflow_job)
    assert isinstance(job, dict)
    assert "if" not in job
    assert "continue-on-error" not in job
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    step_ids = [
        step["id"]
        for step in steps
        if isinstance(step.get("id"), str)
    ]
    assert len(step_ids) == len(set(step_ids))

    test_path = evidence["test"]
    test_step_id = evidence["testStep"]
    assert isinstance(test_step_id, str) and test_step_id
    test_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("id") == test_step_id
    ]
    assert len(test_steps) == 1
    test_index, test_step = test_steps[0]
    assert "if" not in test_step
    assert "continue-on-error" not in test_step
    test_run = test_step.get("run")
    assert isinstance(test_run, str)
    test_tokens = shlex.split(test_run)
    test_command_prefix = schema["testCommandPrefix"]
    assert isinstance(test_command_prefix, list)
    test_evidence_option = schema["testEvidenceOption"]
    assert isinstance(test_evidence_option, str)
    assert test_tokens == [
        *test_command_prefix,
        test_path,
        test_evidence_option,
        generated_paths["resultPath"],
    ]

    report_step_id = evidence["reportStep"]
    assert isinstance(report_step_id, str) and report_step_id
    report_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("id") == report_step_id
    ]
    assert len(report_steps) == 1
    report_index, report_step = report_steps[0]
    assert "if" not in report_step
    assert "continue-on-error" not in report_step
    report_run = report_step.get("run")
    assert isinstance(report_run, str)
    report_tokens = shlex.split(report_run)
    report_command_prefix = schema["reportCommandPrefix"]
    assert isinstance(report_command_prefix, list)
    assert report_tokens[: len(report_command_prefix)] == report_command_prefix
    expected_report_arguments = (
        ("--input", generated_paths["resultPath"]),
        ("--output", generated_paths["reportPath"]),
        ("--integration-id", integration_id),
        ("--test", test_path),
        ("--workflow", evidence["workflow"]),
        ("--workflow-job", workflow_job),
        ("--test-step", test_step_id),
        (
            "--run-id",
            "https://github.com/graphblocks/graphblocks/actions/runs/"
            "${{ github.run_id }}/attempts/${{ github.run_attempt }}",
        ),
        ("--artifact-name", artifact_name),
        ("--candidate-ref", "${{ github.ref }}"),
        ("--candidate-commit", "${{ github.sha }}"),
    )
    expected_report_tokens = list(report_command_prefix)
    for option, expected_value in expected_report_arguments:
        expected_report_tokens.extend((option, expected_value))
    assert report_tokens == expected_report_tokens

    attestation_step_id = evidence["attestationStep"]
    assert isinstance(attestation_step_id, str) and attestation_step_id
    attestation_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("id") == attestation_step_id
    ]
    assert len(attestation_steps) == 1
    attestation_index, attestation_step = attestation_steps[0]
    assert "if" not in attestation_step
    assert "continue-on-error" not in attestation_step
    attestation_run = attestation_step.get("run")
    assert isinstance(attestation_run, str)
    attestation_tokens = shlex.split(attestation_run)
    attestation_prefix = schema["attestationCommandPrefix"]
    assert isinstance(attestation_prefix, list)
    assert attestation_tokens == [
        *attestation_prefix,
        "--bundle",
        generated_paths["signaturePath"],
        generated_paths["reportPath"],
    ]

    upload_action = schema["uploadAction"]
    assert isinstance(upload_action, str) and upload_action.endswith("@")
    upload_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if isinstance(step.get("uses"), str) and step["uses"].startswith(upload_action)
    ]
    assert len(upload_steps) == 1
    upload_index, upload_step = upload_steps[0]
    assert "if" not in upload_step
    assert "continue-on-error" not in upload_step
    upload_uses = upload_step["uses"]
    assert isinstance(upload_uses, str)
    assert re.fullmatch(
        rf"{re.escape(upload_action)}[0-9a-f]{{40}}",
        upload_uses,
    )
    upload_parameters = upload_step.get("with")
    assert isinstance(upload_parameters, dict)
    assert upload_parameters.get("name") == artifact_name
    upload_paths = upload_parameters.get("path")
    assert isinstance(upload_paths, str)
    assert {
        line.strip() for line in upload_paths.splitlines() if line.strip()
    } == {
        generated_paths["reportPath"],
        generated_paths["signaturePath"],
    }
    assert upload_parameters.get("if-no-files-found") == "error"
    assert schema["actionsMustUseCommitSha"] is True
    assert report_index == test_index + 1
    assert attestation_index == report_index + 1
    assert upload_index == attestation_index + 1


def test_documented_rust_toolchain_is_pinned_to_workspace_minimum() -> None:
    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text(encoding="utf-8"))
    rust_version = workspace["workspace"]["package"]["rust-version"]
    expected_channel = rust_version if rust_version.count(".") == 2 else f"{rust_version}.0"

    assert toolchain["toolchain"]["channel"] == expected_channel
    assert toolchain["toolchain"]["profile"] == "minimal"
    assert toolchain["toolchain"]["components"] == ["clippy", "rustfmt"]


def test_ci_enforces_documented_rust_quality_and_packaging_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    wheelhouse_gate = (ROOT / "tools" / "verify_wheelhouse.py").read_text(encoding="utf-8")

    assert "cargo fmt --all -- --check" in workflow
    assert "cargo clippy --workspace --all-targets --locked -- -D warnings" in workflow
    assert "cargo test --workspace --all-targets --locked" in workflow
    assert "cargo package" in workflow
    assert "patch_config=.cargo/config.toml" in workflow
    assert "printf '[patch.crates-io]\\n'" in workflow
    assert '>> "$patch_config"' in workflow
    assert '"${patches[@]}"' not in workflow
    assert '"--no-index"' in wheelhouse_gate
    assert '"--find-links"' in wheelhouse_gate
    assert '"check"' in wheelhouse_gate


def test_rust_packages_declare_publishable_path_versions_and_bundle_local_fixtures() -> None:
    cargo_manifests = sorted((ROOT / "crates").glob("*/Cargo.toml"))
    missing_versions: list[str] = []
    for manifest in cargo_manifests:
        for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
            if 'path = "../' in line and "version =" not in line:
                missing_versions.append(f"{manifest.relative_to(ROOT)}:{line_number}")

    assert not missing_versions, "path dependencies without publishable versions: " + ", ".join(missing_versions)

    fixture_mirrors = {
        "crates/graphblocks-compiler/tests/fixtures/compiler-cases.json": "tck/compiler/cases.json",
        "crates/graphblocks-protocol/tests/fixtures/worker-admission.json": (
            "tck/worker/admission.json"
        ),
        "crates/graphblocks-python/src/fixtures/compiler-cases.json": "tck/compiler/cases.json",
        "crates/graphblocks-python/src/fixtures/runtime-cases.json": "tck/runtime/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/builtin-plugin.yaml": (
            "src/graphblocks/data/builtin-plugin.yaml"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/native-callback-runtime.json": (
            "tck/durable/native-callback-runtime.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/application-events-cases.json": (
            "tck/application-events/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/application-protocol-cases.json": (
            "tck/application-protocol/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/approval-review-cases.json": (
            "tck/approval-review/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/budget-race-cases.json": (
            "tck/budget-race/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/conversation-cases.json": (
            "tck/conversation/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/deployment-cases.json": (
            "tck/deployment/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/documents-cases.json": "tck/documents/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/exhaustion-cases.json": "tck/exhaustion/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/orchestration-cases.json": (
            "tck/orchestration/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/policy-cases.json": "tck/policy/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/rag-cases.json": "tck/rag/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/retry-cases.json": "tck/retry/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/runtime-cases.json": "tck/runtime/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/tool-execution-cases.json": (
            "tck/tool-execution/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/tool-lifecycle-cases.json": (
            "tck/tool-lifecycle/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/tool-result-cases.json": (
            "tck/tool-result/cases.json"
        ),
        "crates/graphblocks-runtime-core/tests/fixtures/usage-cases.json": "tck/usage/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/voice-cases.json": "tck/voice/cases.json",
        "crates/graphblocks-runtime-durable/tests/fixtures/durable-cases.json": "tck/durable/cases.json",
        "crates/graphblocks-runtime-core/tests/fixtures/sequence-cases.json": (
            "tck/sequence/cases.json"
        ),
        "crates/graphblocks-schema/tests/fixtures/cases.json": "tck/schema/cases.json",
        "crates/graphblocks-schema/tests/fixtures/resources.json": "tck/schema/resources.json",
        "crates/graphblocks-schema/tests/fixtures/typed-values.json": "tck/schema/typed-values.json",
    }
    shipped_fixtures = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "crates").glob("*/**/fixtures/*")
        if path.is_file()
    }
    assert shipped_fixtures == set(fixture_mirrors)
    for packaged_path, authoritative_path in fixture_mirrors.items():
        assert (ROOT / packaged_path).read_bytes() == (ROOT / authoritative_path).read_bytes()

    for rust_source in (ROOT / "crates").rglob("*.rs"):
        source = rust_source.read_text(encoding="utf-8")
        assert "../../../tck/" not in source
        assert 'join("../../tck/' not in source


def test_rust_workspace_crate_boundaries_are_documented() -> None:
    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    members = workspace["workspace"]["members"]
    decision = (
        ROOT
        / "docs"
        / "specification"
        / "decisions"
        / "0002-rust-crate-boundaries.md"
    ).read_text(encoding="utf-8")
    retired_crates = {
        "crates/graphblocks-runtime-seq",
        "crates/graphblocks-types",
    }

    assert retired_crates.isdisjoint(members)
    for retired_crate in retired_crates:
        assert not (ROOT / retired_crate / "Cargo.toml").exists()

    workspace_package_names = set()
    for member in members:
        manifest = tomllib.loads(
            (ROOT / member / "Cargo.toml").read_text(encoding="utf-8")
        )
        workspace_package_names.add(manifest["package"]["name"])

    table_rows = []
    in_boundary_table = False
    for line in decision.splitlines():
        if line == "| Crate | Boundary and consumer rationale | Rust API budget |":
            in_boundary_table = True
            continue
        if not in_boundary_table:
            continue
        if line.startswith("| ---"):
            continue
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 3
        crate_cell, rationale, api_budget = cells
        assert crate_cell.startswith("`") and crate_cell.endswith("`")
        assert rationale
        assert api_budget
        table_rows.append(crate_cell[1:-1])

    assert len(table_rows) == len(set(table_rows))
    assert set(table_rows) == workspace_package_names


def test_living_documentation_has_one_authority_tree() -> None:
    assert not (ROOT / "docs" / "upstream").exists()
    assert (ROOT / "docs" / "specification" / "README.md").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "package-catalog.yaml").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").is_file()
    assert (ROOT / "profiles" / "policy-profiles.yaml").is_file()


def test_control_plane_cli_name_and_binding_boundary_are_explicit() -> None:
    control_plane = tomllib.loads(
        (ROOT / "crates" / "graphblocksd" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
    )
    assert control_plane["package"]["name"] == "graphblocks-control-plane"
    assert control_plane["package"]["description"] == (
        "Reusable GraphBlocks control-plane contracts and one-shot "
        "graphblocks-control CLI"
    )
    assert control_plane["lib"]["name"] == "graphblocks_control_plane"
    assert control_plane["bin"] == [
        {"name": "graphblocks-control", "path": "src/main.rs"}
    ]

    control_source = (
        ROOT / "crates" / "graphblocksd" / "src" / "main.rs"
    ).read_text(encoding="utf-8")
    assert '"usage: graphblocks-control <' in control_source
    assert '"usage: graphblocksd <' not in control_source

    binding = tomllib.loads(
        (ROOT / "crates" / "graphblocks-python" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
    )
    dependencies = binding["dependencies"]
    assert "graphblocks-control-plane" in dependencies
    assert dependencies["graphblocks-control-plane"] == {
        "version": "0.1.0",
        "path": "../graphblocksd",
    }
    assert "graphblocksd" not in dependencies

    documented_surfaces = [
        ROOT / "README.md",
        ROOT / "README.ko.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "getting-started" / "installation.md",
        ROOT / "docs" / "project" / "first-stable-release.md",
        ROOT / "docs" / "project" / "status.md",
    ]
    for document in documented_surfaces:
        content = document.read_text(encoding="utf-8")
        assert "`graphblocks-control`" in content
        assert "`graphblocksd`" not in content


def test_real_adapter_evidence_binds_test_workflow_revision_and_run(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "tests" / "integration" / "test_qdrant_real_service.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_real_service():\n    pass\n", encoding="utf-8")
    workflow_path = tmp_path / ".github" / "workflows" / "real-services.yml"
    workflow_path.parent.mkdir(parents=True)
    valid_workflow = """\
jobs:
  qdrant:
    steps:
      - id: exercise-qdrant-real-service
        name: Exercise the real service and write evidence
        run: >-
          python -m pytest tests/integration/test_qdrant_real_service.py
          --real-service-evidence
          .artifacts/real-service/graphblocks-qdrant-result.json
      - id: freeze-qdrant-report
        run: >-
          python tools/release_supply_chain.py freeze-integration-report
          --input .artifacts/real-service/graphblocks-qdrant-result.json
          --output .artifacts/real-service/graphblocks-qdrant-report.json
          --integration-id graphblocks-qdrant
          --test tests/integration/test_qdrant_real_service.py
          --workflow .github/workflows/real-services.yml
          --workflow-job qdrant
          --test-step exercise-qdrant-real-service
          --run-id "https://github.com/graphblocks/graphblocks/actions/runs/${{ github.run_id }}/attempts/${{ github.run_attempt }}"
          --artifact-name "graphblocks-qdrant-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
          --candidate-ref "${{ github.ref }}"
          --candidate-commit "${{ github.sha }}"
      - id: attest-qdrant-result
        run: >-
          cosign sign-blob --yes
          --bundle .artifacts/real-service/graphblocks-qdrant-report.sigstore.json
          .artifacts/real-service/graphblocks-qdrant-report.json
      - uses: actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f
        with:
          name: graphblocks-qdrant-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}
          path: |
            .artifacts/real-service/graphblocks-qdrant-report.json
            .artifacts/real-service/graphblocks-qdrant-report.sigstore.json
          if-no-files-found: error
"""
    workflow_path.write_text(valid_workflow, encoding="utf-8")
    schema: dict[str, object] = {
        "requiredFields": [
            "test",
            "workflow",
            "workflowJob",
            "testStep",
            "reportStep",
            "attestationStep",
            "artifactName",
            "resultPath",
            "reportPath",
            "signaturePath",
        ],
        "pathsMustExist": True,
        "testPathPrefix": "tests/",
        "workflowPathPrefix": ".github/workflows/",
        "artifactPathPrefix": ".artifacts/real-service/",
        "artifactNameBindings": [
            "${{ github.sha }}",
            "${{ github.run_id }}",
            "${{ github.run_attempt }}",
        ],
        "testCommandPrefix": ["python", "-m", "pytest"],
        "testEvidenceOption": "--real-service-evidence",
        "reportCommandPrefix": [
            "python",
            "tools/release_supply_chain.py",
            "freeze-integration-report",
        ],
        "attestationCommandPrefix": ["cosign", "sign-blob", "--yes"],
        "uploadAction": "actions/upload-artifact@",
        "actionsMustUseCommitSha": True,
    }
    evidence = {
        "test": "tests/integration/test_qdrant_real_service.py",
        "workflow": ".github/workflows/real-services.yml",
        "workflowJob": "qdrant",
        "testStep": "exercise-qdrant-real-service",
        "reportStep": "freeze-qdrant-report",
        "attestationStep": "attest-qdrant-result",
        "artifactName": (
            "graphblocks-qdrant-${{ github.sha }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}"
        ),
        "resultPath": ".artifacts/real-service/graphblocks-qdrant-result.json",
        "reportPath": ".artifacts/real-service/graphblocks-qdrant-report.json",
        "signaturePath": (
            ".artifacts/real-service/graphblocks-qdrant-report.sigstore.json"
        ),
    }

    _validate_real_service_evidence(
        tmp_path,
        "graphblocks-qdrant",
        evidence,
        schema,
    )

    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            {**evidence, "test": "/etc/passwd"},
            schema,
        )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            {
                **evidence,
                "artifactName": (
                    "graphblocks-qdrant-${{ github.sha }}-"
                    "${{ github.run_id }}"
                ),
            },
            schema,
        )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            {**evidence, "workflowJob": "unrelated"},
            schema,
        )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            {**evidence, "attestationStep": "missing"},
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            "python -m pytest tests/integration/test_qdrant_real_service.py",
            (
                "echo tests/integration/test_qdrant_real_service.py "
                ".artifacts/real-service/graphblocks-qdrant-result.json"
            ),
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            "id: exercise-qdrant-real-service",
            "id: exercise-qdrant-real-service\n        if: false",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            '          --candidate-commit "${{ github.sha }}"',
            (
                '          --candidate-commit "${{ github.sha }}"\n'
                '          python -c "open('
                "'.artifacts/real-service/graphblocks-qdrant-report.json', "
                "'w').write('{}')\""
            ),
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            "      - id: attest-qdrant-result",
            (
                "      - id: overwrite-qdrant-report\n"
                "        run: echo '{}' > "
                ".artifacts/real-service/graphblocks-qdrant-report.json\n"
                "      - id: attest-qdrant-result"
            ),
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            '"${{ github.sha }}"',
            '"${{ github.sha }}$(printf forged)"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )

    workflow_path.write_text(
        valid_workflow.replace(
            "tests/integration/test_qdrant_real_service.py",
            "tests/integration/test_unrelated.py",
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _validate_real_service_evidence(
            tmp_path,
            "graphblocks-qdrant",
            evidence,
            schema,
        )


def test_profile_release_tracks_are_closed_owned_and_independent() -> None:
    matrix = yaml.safe_load(
        (ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text(
            encoding="utf-8"
        )
    )
    catalog_path = (
        ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml"
    )
    conformance_catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    profiles = {entry["id"]: entry for entry in matrix["profiles"]}
    catalog_profile_ids = {
        entry["id"] for entry in conformance_catalog["spec"]["profiles"]
    }
    assert set(profiles) == catalog_profile_ids

    policy = matrix["profileReleasePolicy"]
    assert policy["readiness"] == "boundary-candidate-enforced"
    assert policy["scope"] == "release-boundary-metadata-only"
    assert policy["conformanceCatalog"] == (
        "src/graphblocks/data/conformance-profiles.yaml"
    )
    assert (ROOT / policy["conformanceCatalog"]).resolve() == catalog_path.resolve()
    assert policy["componentCatalog"] == "src/graphblocks/data/package-catalog.yaml"
    component_catalog_path = ROOT / policy["componentCatalog"]
    component_catalog = yaml.safe_load(
        component_catalog_path.read_text(encoding="utf-8")
    )
    assert policy["recordsAuthority"] == "profiles"
    assert policy["stableTargetReleaseTrack"] == "core"
    assert policy["artifactRoleDefinitions"] == {
        "claimOwnerArtifact": (
            "compatibility-claim-accountability-not-exclusive-implementation"
        ),
        "implementationArtifacts": (
            "shipped-code-participating-in-profile-implementation"
        ),
        "evidenceArtifacts": (
            "packaged-conformance-evidence-producers-not-complete-release-evidence"
        ),
    }
    assert policy["authorityInheritanceMode"] == (
        "transitive-role-preserving-from-extends"
    )
    required_fields = {
        "releaseTrack",
        "catalogReleaseTrain",
        "claimOwnerArtifact",
        "implementationArtifacts",
        "evidenceArtifacts",
        "authority",
        "extends",
        "tier",
        "promotionGate",
        "requiredGates",
    }
    assert set(policy["requiredFields"]) == required_fields
    assert policy["extensionRequiredFields"] == ["extensionTrack"]
    assert policy["externalIntegrationTrack"] == "integrationPromotionPolicy"

    tracks = policy["tracks"]
    assert set(tracks) == {"core", "extension"}
    core_profile_ids = set(tracks["core"]["profileIds"])
    extension_profile_ids = set(tracks["extension"]["profileIds"])
    assert core_profile_ids == {"GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME"}
    assert not core_profile_ids & extension_profile_ids
    assert core_profile_ids | extension_profile_ids == set(profiles)
    assert tracks["core"]["allowedTiers"] == ["stable"]
    assert tracks["core"]["promotionGate"] == "REL-CORE-PROFILE"
    assert tracks["core"]["includedInTargetRelease"] is True
    assert tracks["core"]["eligibility"] == {
        "portableExecutionSemanticsRequired": True,
        "minimumIndependentRuntimeImplementations": 2,
        "providerNeutralTckRequired": True,
        "providerDatabaseDeploymentPolicyAllowed": False,
    }
    assert tracks["extension"]["allowedTiers"] == ["preview"]
    assert tracks["extension"]["promotionGate"] == "REL-EXTENSION-PROFILE"
    assert tracks["extension"]["includedInTargetRelease"] is False
    assert tracks["extension"]["independentPromotionRequired"] is True
    assert tracks["extension"]["ancestorPromotionRequired"] is True
    assert tracks["extension"]["ancestorRequiredGateMode"] == "transitive-union"
    assert tracks["extension"]["promotionEvidenceIdentity"] == "profile-id-bound"
    assert tracks["extension"]["shippedSecurityDefectsRemainReleaseBlocking"] is True

    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    release_gates = {entry["id"]: entry for entry in matrix["releaseGates"]}
    for track_name, track in tracks.items():
        track_profile_ids = set(track["profileIds"])
        assert track_profile_ids == {
            profile_id
            for profile_id, profile in profiles.items()
            if profile["releaseTrack"] == track_name
        }
        for profile_id in track_profile_ids:
            profile = profiles[profile_id]
            assert required_fields <= profile.keys()
            assert profile["tier"] in track["allowedTiers"]
            assert profile["promotionGate"] == track["promotionGate"]
            assert profile["promotionGate"] in profile["requiredGates"]
            assert profile["promotionGate"] in release_gates
            claim_owner_artifact = profile["claimOwnerArtifact"]
            implementation_artifacts = profile["implementationArtifacts"]
            evidence_artifacts = profile["evidenceArtifacts"]
            assert claim_owner_artifact in artifacts
            assert isinstance(implementation_artifacts, list)
            assert implementation_artifacts
            assert len(implementation_artifacts) == len(
                set(implementation_artifacts)
            )
            assert claim_owner_artifact in implementation_artifacts
            assert set(implementation_artifacts) <= set(artifacts)
            assert isinstance(evidence_artifacts, list) and evidence_artifacts
            assert len(evidence_artifacts) == len(set(evidence_artifacts))
            assert set(evidence_artifacts) <= set(artifacts)
            assert not set(implementation_artifacts) & set(evidence_artifacts)
            authority = profile["authority"]
            assert isinstance(authority, dict) and authority
            assert all(
                isinstance(role, str)
                and role
                and isinstance(implementation, str)
                and implementation
                for role, implementation in authority.items()
            )
            assert all(
                role.startswith(("active", "target", "reference", "inherited"))
                for role in authority
            )

    catalog_profiles = {
        entry["id"]: entry for entry in conformance_catalog["spec"]["profiles"]
    }
    tier_rank = {"preview": 0, "stable": 1}
    ancestors_by_profile: dict[str, set[str]] = {}
    for profile_id, profile in profiles.items():
        catalog_extends = catalog_profiles[profile_id].get("extends", [])
        assert profile["extends"] == catalog_extends
        ancestors: set[str] = set()
        pending_ancestors = list(catalog_extends)
        while pending_ancestors:
            ancestor_id = pending_ancestors.pop()
            if ancestor_id in ancestors:
                continue
            assert ancestor_id in profiles
            ancestors.add(ancestor_id)
            pending_ancestors.extend(catalog_profiles[ancestor_id].get("extends", []))
        assert profile_id not in ancestors
        ancestors_by_profile[profile_id] = ancestors
        assert all(
            tier_rank[profile["tier"]] <= tier_rank[profiles[ancestor_id]["tier"]]
            for ancestor_id in ancestors
        )
        assert {
            gate
            for ancestor_id in ancestors
            for gate in profiles[ancestor_id]["requiredGates"]
        } <= set(profile["requiredGates"])
        for ancestor_id in ancestors:
            for role, implementation in profiles[ancestor_id]["authority"].items():
                if (
                    role in profile["authority"]
                    and role != "inheritedAuthorityFrom"
                ):
                    assert profile["authority"][role] == implementation

    assert profiles["GB-C0-SCHEMA"]["authority"] == {
        "activeCompiler": "rust",
        "activeAuthoringFacade": "python",
        "referenceOracle": "python",
        "targetStandaloneCanonicalAndSchema": "rust-transition-blocked",
    }
    assert profiles["GB-C1-LOCAL-RUNTIME"]["authority"] == {
        "activeCompiler": "rust",
        "activeReferenceInterpreter": "python",
        "targetProductionScheduler": "rust-transition-blocked",
        "inheritedAuthorityFrom": "extends",
    }
    for profile_id in extension_profile_ids:
        extension_authority = profiles[profile_id]["authority"]
        assert extension_authority["activeContract"] == "specification"
        assert extension_authority["activeReferenceImplementation"] == "python"
        assert extension_authority["inheritedAuthorityFrom"] == "extends"
        resolved_authority = {
            (source_profile_id, role): implementation
            for source_profile_id in {
                profile_id,
                *ancestors_by_profile[profile_id],
            }
            for role, implementation in profiles[source_profile_id][
                "authority"
            ].items()
        }
        assert resolved_authority[
            ("GB-C1-LOCAL-RUNTIME", "activeReferenceInterpreter")
        ] == "python"
        assert resolved_authority[
            ("GB-C1-LOCAL-RUNTIME", "targetProductionScheduler")
        ] == "rust-transition-blocked"
        assert resolved_authority[("GB-C0-SCHEMA", "activeCompiler")] == "rust"
    assert profiles["GB-C4-PRODUCTION"]["authority"][
        "targetProductionRuntime"
    ] == "rust-transition-blocked"
    assert profiles["GB-X3-DURABLE-STREAM"]["authority"][
        "targetDurableRuntime"
    ] == "rust-transition-blocked"

    release_trains = component_catalog["releaseTrains"]
    assert set(release_trains) == {
        "core",
        "aiApplication",
        "governance",
        "productionPlatform",
        "orchestration",
        "voice",
        "durableStream",
        "integrations",
    }
    expected_train_profiles = {
        "core": core_profile_ids,
        "aiApplication": {"GB-C2-AI-APPLICATION"},
        "governance": {"GB-C3-GOVERNED-RUNTIME"},
        "productionPlatform": {"GB-C4-PRODUCTION"},
        "orchestration": {"GB-X1-ORCHESTRATION"},
        "voice": {"GB-X2-VOICE"},
        "durableStream": {"GB-X3-DURABLE-STREAM"},
        "integrations": set(),
    }
    for train_name, expected_profiles in expected_train_profiles.items():
        train = release_trains[train_name]
        assert set(train["profiles"]) == expected_profiles
        assert train["promotionGate"] in release_gates
        if train_name == "core":
            assert train["minorVersionCoordinated"] is True
        else:
            assert train["minorVersionCoordinated"] is False
        if expected_profiles:
            assert {
                profiles[profile_id]["catalogReleaseTrain"]
                for profile_id in expected_profiles
            } == {train_name}
            assert {
                profiles[profile_id]["promotionGate"]
                for profile_id in expected_profiles
            } == {train["promotionGate"]}
    assert release_trains["integrations"]["promotionGate"] == (
        "REL-INTEGRATION-PROMOTION"
    )

    component_records = {
        entry["name"]: entry for entry in component_catalog["components"]
    }
    component_required_profiles: dict[str, set[str]] = {}
    cross_track_surfaces: dict[str, dict[str, list[str]]] = {}
    cross_track_requirements: dict[str, dict[str, object]] = {}
    for train_name, train in release_trains.items():
        required_profiles = train["componentRequiredProfiles"]
        assert set(required_profiles) == set(train["components"])
        train_cross_track_surfaces = train.get("crossTrackSurfaces", {})
        assert set(train_cross_track_surfaces) <= set(train["components"])
        cross_track_surfaces.update(train_cross_track_surfaces)
        train_cross_track_requirements = train.get("crossTrackRequirements", {})
        assert set(train_cross_track_requirements) <= set(train["components"])
        cross_track_requirements.update(train_cross_track_requirements)
        for component, profile_ids in required_profiles.items():
            assert isinstance(profile_ids, list) and profile_ids
            assert len(profile_ids) == len(set(profile_ids))
            assert set(profile_ids) <= set(profiles)
            if (
                train_name != "integrations"
                and component not in train_cross_track_surfaces
                and component not in train_cross_track_requirements
            ):
                assert set(profile_ids) <= set(train["profiles"])
            if component in train_cross_track_surfaces:
                assert set(profile_ids) == {
                    profile_id
                    for surface_profile_ids in train_cross_track_surfaces[
                        component
                    ].values()
                    for profile_id in surface_profile_ids
                }
            if component in train_cross_track_requirements:
                requirement = train_cross_track_requirements[component]
                owning_profile = requirement["owningProfile"]
                additional_profiles = requirement["additionalRequiredProfiles"]
                assert isinstance(owning_profile, str)
                assert isinstance(additional_profiles, list)
                assert all(
                    isinstance(profile_id, str)
                    for profile_id in additional_profiles
                )
                assert set(profile_ids) == {
                    owning_profile,
                    *additional_profiles,
                }
            component_required_profiles[component] = set(profile_ids)
    assert set(component_required_profiles) == set(component_records)
    assert cross_track_requirements == {
        "graphblocks-agents": {
            "owningProfile": "GB-C2-AI-APPLICATION",
            "additionalRequiredProfiles": ["GB-C3-GOVERNED-RUNTIME"],
        }
    }
    assert cross_track_surfaces == {
        "graphblocks-cli": {
            "stableCoreCommands": ["GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME"],
            "previewProductionCommands": ["GB-C4-PRODUCTION"],
        }
    }
    for surface_name, profile_ids in cross_track_surfaces[
        "graphblocks-cli"
    ].items():
        expected_tier = (
            "stable" if surface_name.startswith("stable") else "preview"
        )
        assert all(
            profiles[profile_id]["tier"] == expected_tier
            for profile_id in profile_ids
        )

    assert {
        component: component_required_profiles[component]
        for component in release_trains["integrations"]["components"]
    } == {
        "graphblocks-pdf": {"GB-C2-AI-APPLICATION"},
        "graphblocks-qdrant": {"GB-C2-AI-APPLICATION"},
        "graphblocks-mcp": {"GB-C1-LOCAL-RUNTIME"},
        "graphblocks-openapi": {"GB-C1-LOCAL-RUNTIME"},
        "graphblocks-openai": {"GB-C2-AI-APPLICATION"},
        "graphblocks-haystack": {"GB-C2-AI-APPLICATION"},
        "graphblocks-policy-opa": {"GB-C3-GOVERNED-RUNTIME"},
        "graphblocks-policy-cedar": {"GB-C3-GOVERNED-RUNTIME"},
        "graphblocks-budget-postgres": {"GB-C3-GOVERNED-RUNTIME"},
        "graphblocks-usage-postgres": {"GB-C3-GOVERNED-RUNTIME"},
        "graphblocks-kubernetes": {"GB-C4-PRODUCTION"},
        "graphblocks-terraform": {"GB-C4-PRODUCTION"},
        "graphblocks-oci": {"GB-C4-PRODUCTION"},
        "graphblocks-gitops": {"GB-C4-PRODUCTION"},
        "graphblocks-otel": {"GB-C4-PRODUCTION"},
        "graphblocks-langfuse": {"GB-C4-PRODUCTION"},
        "graphblocks-prometheus": {"GB-C4-PRODUCTION"},
        "graphblocks-dashboards": {"GB-C4-PRODUCTION"},
        "graphblocks-webrtc": {"GB-X2-VOICE"},
        "graphblocks-websocket-media": {"GB-X2-VOICE"},
        "graphblocks-openai-realtime": {"GB-X2-VOICE"},
        "graphblocks-silero-vad": {"GB-X2-VOICE"},
        "graphblocks-kafka": {"GB-X3-DURABLE-STREAM"},
        "graphblocks-nats": {"GB-X3-DURABLE-STREAM"},
        "graphblocks-sqs": {"GB-X3-DURABLE-STREAM"},
        "graphblocks-pubsub": {"GB-X3-DURABLE-STREAM"},
        "graphblocks-scripted": {"GB-C2-AI-APPLICATION"},
    }

    for component, component_record in component_records.items():
        source_profile_closure = {
            profile_id
            for required_profile_id in component_required_profiles[component]
            for profile_id in {
                required_profile_id,
                *ancestors_by_profile[required_profile_id],
            }
        }
        for dependency in component_record.get("dependsOn", []):
            assert component_required_profiles[dependency] <= source_profile_closure

    integration_tiers = {
        entry["id"]: entry["tier"] for entry in matrix["integrations"]
    }
    for component in release_trains["integrations"]["components"]:
        if integration_tiers[component] == "stable":
            assert all(
                profiles[profile_id]["tier"] == "stable"
                for profile_id in component_required_profiles[component]
            )

    component_memberships = Counter(
        component
        for train in release_trains.values()
        for component in train["components"]
    )
    assert component_memberships == Counter(
        {component_name: 1 for component_name in component_records}
    )
    assert set(release_trains["integrations"]["components"]) == set(
        component_catalog["integrationRules"]["maturityManagedComponents"]
    )
    assert all(
        component_records[component]["stability"] == "foundation"
        for component in release_trains["core"]["components"]
    )
    assert all(
        component_records[component]["stability"] != "foundation"
        for train_name in (
            "aiApplication",
            "governance",
            "productionPlatform",
            "orchestration",
            "voice",
            "durableStream",
        )
        for component in release_trains[train_name]["components"]
    )
    production_train = release_trains["productionPlatform"]
    assert set(production_train["domains"]) == {
        "runtime-service",
        "deployment",
        "observability",
        "tooling",
    }
    assert Counter(
        component
        for domain_components in production_train["domains"].values()
        for component in domain_components
    ) == Counter({component: 1 for component in production_train["components"]})
    assert any(
        "crosses release tracks" in note
        for note in component_catalog["defaultSelection"]["notes"]
    )

    assert all(
        "extensionTrack" not in profiles[profile_id]
        for profile_id in core_profile_ids
    )
    assert {
        profiles[profile_id]["extensionTrack"]
        for profile_id in extension_profile_ids
    } == {
        "ai-application",
        "governance",
        "production-platform",
        "orchestration",
        "voice",
        "durable-stream",
    }
    stable_claimed_profiles = {
        profile_id
        for artifact in artifacts.values()
        for profile_id in artifact.get("stableProfiles", [])
    }
    assert stable_claimed_profiles == core_profile_ids
    assert extension_profile_ids <= set(artifacts["pypi:graphblocks"]["exclusions"])
    assert set(artifacts["pypi:graphblocks-runtime"]["previewSurfaces"]) == (
        extension_profile_ids
    )

    core_gate = release_gates["REL-CORE-PROFILE"]
    extension_gate = release_gates["REL-EXTENSION-PROFILE"]
    assert core_gate["releaseTrack"] == "core"
    assert core_gate["blocksTargetRelease"] is True
    assert core_gate["id"] in matrix["globalRequiredGates"]
    assert extension_gate["releaseTrack"] == "extension"
    assert extension_gate["readiness"] == "definition-blocked"
    assert extension_gate["blocksTargetRelease"] is False
    assert extension_gate["independentPromotionRequired"] is True
    assert extension_gate["ancestorPromotionRequired"] is True
    assert extension_gate["ancestorRequiredGateMode"] == "transitive-union"
    assert extension_gate["profileIdentityBoundEvidenceRequired"] is True
    assert extension_gate["blockers"] == [
        "profile-scoped-promotion-evidence-contract-not-frozen"
    ]
    assert extension_gate["id"] not in matrix["globalRequiredGates"]
    expected_evidence = {
        "src/graphblocks/data/conformance-profiles.yaml",
        "docs/project/stable-release-matrix.yaml",
        "docs/specification/conformance/profiles.md",
        "tests/test_documentation_integrity.py",
    }
    assert set(core_gate["evidence"]) == expected_evidence
    assert set(extension_gate["evidence"]) == expected_evidence
    assert all((ROOT / path).is_file() for path in expected_evidence)


def test_stable_release_matrix_is_complete_and_machine_readable() -> None:
    matrix = yaml.safe_load((ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text())
    assert matrix["matrixVersion"] == 1
    assert matrix["targetRelease"] == "1.0"
    assert matrix["currentReadiness"] == "blocked"

    tiers = {"stable", "preview", "internal", "reserved"}
    assert set(matrix["tierDefinitions"]) == tiers
    for section in ("artifacts", "profiles", "wireVersions", "integrations"):
        entries = matrix[section]
        identities = [entry["id"] for entry in entries]
        assert len(identities) == len(set(identities)), f"duplicate {section} identity"
        assert all(entry["tier"] in tiers for entry in entries)

    artifacts = {entry["id"]: entry for entry in matrix["artifacts"]}
    for artifact in artifacts.values():
        if path := artifact.get("path"):
            assert (ROOT / path).is_file(), f"missing release-matrix artifact path: {path}"
        if source := artifact.get("source"):
            assert source in artifacts, f"unknown release-matrix artifact source: {source}"

    workspace = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    workspace_crates = set()
    for member in workspace["workspace"]["members"]:
        manifest = tomllib.loads(
            (ROOT / member / "Cargo.toml").read_text(encoding="utf-8")
        )
        workspace_crates.add(f"crate:{manifest['package']['name']}")
    assert workspace_crates <= set(artifacts)
    assert "crate:graphblocks-control-plane" in artifacts
    assert "crate:graphblocksd" not in artifacts
    assert "executable:graphblocksd" not in artifacts
    control_cli = artifacts["executable:graphblocks-control"]
    assert control_cli == {
        "id": "executable:graphblocks-control",
        "ecosystem": "native",
        "kind": "executable",
        "source": "crate:graphblocks-control-plane",
        "tier": "internal",
        "readiness": "implemented-internal",
        "binaryName": "graphblocks-control",
        "commandMode": "one-shot",
        "transport": "local-process",
        "requestArguments": "argv-options",
        "stdinPayloadFormat": "json",
        "stdinPayloadMode": "command-specific",
        "stdinJsonPayloadCommands": [
            "admit-worker-message",
            "submit-async-callback",
            "quarantine-async-callback",
        ],
        "successResponse": "stdout-json",
        "errorResponse": "stderr-json",
        "networkListener": False,
        "serveCommand": False,
        "capabilities": [
            "worker-message-admission",
            "run-lease-and-status-transitions",
            "async-callback-lifecycle",
            "callback-delivery-lifecycle",
            "checkpoint-claim-lifecycle",
        ],
        "nonCapabilities": [
            "long-running-daemon",
            "http-server",
            "supervisor-lifecycle",
        ],
    }
    control_manifest = tomllib.loads(
        (ROOT / artifacts[control_cli["source"]]["path"]).read_text(encoding="utf-8")
    )
    assert [target["name"] for target in control_manifest["bin"]] == [
        control_cli["binaryName"]
    ]
    control_source = (
        ROOT / "crates" / "graphblocksd" / "src" / "main.rs"
    ).read_text(encoding="utf-8")
    stdin_contract = re.search(
        r"const JSON_STDIN_PAYLOAD_COMMANDS: \[&str; \d+\] = \[(.*?)\];",
        control_source,
        re.DOTALL,
    )
    assert stdin_contract is not None
    implementation_stdin_commands = re.findall(r'"([a-z-]+)"', stdin_contract.group(1))
    assert implementation_stdin_commands == control_cli["stdinJsonPayloadCommands"]
    for command in implementation_stdin_commands:
        assert control_source.count(f'read_json_stdin_payload("{command}")') == 1
    assert control_source.count("read_json_stdin_payload(") == (
        len(implementation_stdin_commands) + 1
    )

    assert "helm:graphblocks-operator" not in artifacts
    deployment_chart = artifacts["helm:graphblocks-deployment-chart"]
    assert deployment_chart == {
        "id": "helm:graphblocks-deployment-chart",
        "ecosystem": "kubernetes",
        "kind": "helm-scaffold",
        "path": "packages/graphblocks-deployment-chart/Chart.yaml",
        "tier": "internal",
        "readiness": "scaffold-only",
        "controllerIncluded": False,
        "ociImageIncluded": False,
        "defaultEnabled": False,
        "requiredProfile": "GB-C4-PRODUCTION",
        "implementsProfile": False,
        "promotionGate": "REL-KUBERNETES-OPERATOR",
    }
    chart = yaml.safe_load(
        (ROOT / deployment_chart["path"]).read_text(encoding="utf-8")
    )
    assert chart["name"] == "graphblocks-deployment-chart"
    assert chart["annotations"] == {
        "graphblocks.ai/artifact-kind": "deployment-scaffold",
        "graphblocks.ai/controller-included": "false",
        "graphblocks.ai/maturity": "internal",
    }
    assert all(
        deployment_chart["id"] not in profile["implementationArtifacts"]
        for profile in matrix["profiles"]
    )

    profile_catalog = yaml.safe_load(
        (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").read_text()
    )
    catalog_profiles = {entry["id"] for entry in profile_catalog["spec"]["profiles"]}
    assert {entry["id"] for entry in matrix["profiles"]} == catalog_profiles

    stable_wires = [entry for entry in matrix["wireVersions"] if entry["tier"] == "stable"]
    assert {entry["id"] for entry in stable_wires} == {
        "graphblocks.ai/v1:Graph",
        "graphblocks.ai/v1:PluginManifest",
    }
    assert all(entry["readiness"] == "candidate-enforced" for entry in stable_wires)

    catalog = yaml.safe_load(
        (ROOT / "src" / "graphblocks" / "data" / "package-catalog.yaml").read_text()
    )
    catalog_component_names = {entry["name"] for entry in catalog["components"]}
    integration_namespace_components = {
        entry["name"]
        for entry in catalog["components"]
        if isinstance(entry["import"], str)
        and entry["import"].startswith("graphblocks.integrations.")
    }
    maturity_managed_components = catalog["integrationRules"][
        "maturityManagedComponents"
    ]
    assert len(maturity_managed_components) == len(set(maturity_managed_components))
    maturity_managed_component_ids = set(maturity_managed_components)
    assert maturity_managed_component_ids <= catalog_component_names
    assert integration_namespace_components <= maturity_managed_component_ids
    assert "graphblocks-dashboards" in maturity_managed_component_ids

    integration_entries = matrix["integrations"]
    matrix_integrations = {entry["id"] for entry in integration_entries}

    promotion = matrix["integrationPromotionPolicy"]
    assert promotion["readiness"] == "candidate-enforced"
    assert promotion["componentCatalog"] == "src/graphblocks/data/package-catalog.yaml"
    assert (ROOT / promotion["componentCatalog"]).is_file()
    assert promotion["recordsAuthority"] == "integrations"
    assert promotion["promotionGate"] == "REL-INTEGRATION-PROMOTION"
    synthetic_test_doubles = promotion["syntheticTestDoubles"]
    assert synthetic_test_doubles == [
        "repository-fakes",
        "acceptance-harness-adapters",
    ]
    assert not set(synthetic_test_doubles) & catalog_component_names
    assert matrix_integrations == (
        maturity_managed_component_ids | set(synthetic_test_doubles)
    )
    required_integration_fields = {
        "implementationMaturity",
        "supportedAuthentication",
        "supportedServiceOrSdkVersions",
        "realServiceEvidence",
        "retryAndFailureModel",
        "promotionGate",
    }
    assert set(promotion["requiredFields"]) == required_integration_fields
    allowed_maturity = {"contract-only", "test-double", "real-adapter"}
    assert set(promotion["allowedMaturity"]) == allowed_maturity
    allowed_authentication = {
        "none",
        "api-key",
        "basic",
        "bearer",
        "oauth2",
        "mtls",
        "ambient-cloud-identity",
        "transport-supplied",
    }
    assert set(promotion["allowedAuthentication"]) == allowed_authentication
    assert promotion["supportCoverage"] == (
        "authentication-version-cartesian-product"
    )
    evidence_schema = promotion["realServiceEvidenceSchema"]
    assert isinstance(evidence_schema, dict)
    assert evidence_schema["kind"] == "signed-workflow-recipe"
    evidence_fields = {
        "test",
        "workflow",
        "workflowJob",
        "testStep",
        "reportStep",
        "attestationStep",
        "artifactName",
        "resultPath",
        "reportPath",
        "signaturePath",
    }
    assert set(evidence_schema["requiredFields"]) == evidence_fields
    assert evidence_schema["pathsMustExist"] is True
    assert evidence_schema["testPathPrefix"] == "tests/"
    assert evidence_schema["workflowPathPrefix"] == ".github/workflows/"
    assert evidence_schema["artifactPathPrefix"] == ".artifacts/real-service/"
    assert evidence_schema["artifactNameBindings"] == [
        "${{ github.sha }}",
        "${{ github.run_id }}",
        "${{ github.run_attempt }}",
    ]
    assert evidence_schema["testCommandPrefix"] == ["python", "-m", "pytest"]
    assert evidence_schema["testEvidenceOption"] == "--real-service-evidence"
    assert evidence_schema["reportCommandPrefix"] == [
        "python",
        "tools/release_supply_chain.py",
        "freeze-integration-report",
    ]
    assert evidence_schema["attestationCommandPrefix"] == [
        "cosign",
        "sign-blob",
        "--yes",
    ]
    assert evidence_schema["uploadAction"] == "actions/upload-artifact@"
    assert evidence_schema["actionsMustUseCommitSha"] is True
    assert evidence_schema["criticalStepsMustBeContiguous"] is True
    promotion_report_schema = evidence_schema["promotionReport"]
    assert promotion_report_schema["releaseEvidenceField"] == "integrationRuns"
    assert promotion_report_schema["reportReferenceField"] == "reportDigest"
    assert set(promotion_report_schema["requiredFields"]) == {
        "integrationId",
        "status",
        "complete",
        "candidateRef",
        "candidateCommit",
        "test",
        "workflow",
        "workflowJob",
        "testStep",
        "runId",
        "artifactName",
        "result",
        "resultDigest",
    }
    assert promotion_report_schema["candidateRevisionBound"] is True
    assert promotion_report_schema["candidateFinalClaimEquality"] is True
    assert promotion_report_schema["signedByReferencedWorkflow"] is True
    assert promotion_report_schema["signatureBundleRequired"] is True

    for entry in integration_entries:
        assert required_integration_fields <= entry.keys(), entry["id"]
        maturity = entry["implementationMaturity"]
        assert maturity in allowed_maturity
        for field in (
            "supportedAuthentication",
            "supportedServiceOrSdkVersions",
        ):
            values = entry[field]
            assert isinstance(values, list), (entry["id"], field)
            assert all(isinstance(value, str) and value for value in values)
        assert set(entry["supportedAuthentication"]) <= allowed_authentication
        real_service_evidence = entry["realServiceEvidence"]
        assert isinstance(real_service_evidence, list), entry["id"]
        for evidence in real_service_evidence:
            _validate_real_service_evidence(
                ROOT,
                entry["id"],
                evidence,
                evidence_schema,
            )
        assert (
            isinstance(entry["retryAndFailureModel"], str)
            and entry["retryAndFailureModel"]
        )
        assert entry["promotionGate"] == promotion["promotionGate"]
        if maturity == "test-double":
            assert entry["tier"] == "internal"
            assert entry["realServiceEvidence"] == []
        elif maturity == "contract-only":
            assert entry["tier"] != "stable"
            assert entry["realServiceEvidence"] == []
        else:
            assert entry["supportedAuthentication"]
            assert entry["supportedServiceOrSdkVersions"]
            assert real_service_evidence
            assert len(real_service_evidence) >= (
                len(entry["supportedAuthentication"])
                * len(entry["supportedServiceOrSdkVersions"])
            )
            assert entry["retryAndFailureModel"] not in {
                "caller-owned",
                "deterministic-test-only",
            }
        if entry["tier"] == "stable":
            assert maturity == "real-adapter"

    real_adapter_ids = {
        entry["id"]
        for entry in integration_entries
        if entry["implementationMaturity"] == "real-adapter"
    }
    assert real_adapter_ids == set()
    status = (ROOT / "docs" / "project" / "status.md").read_text(encoding="utf-8")
    first_stable = (ROOT / "docs" / "project" / "first-stable-release.md").read_text(
        encoding="utf-8"
    )
    assert "There are no `real-adapter` claims." in status
    assert "contains no `real-adapter` entry." in first_stable

    gate_ids = [entry["id"] for entry in matrix["releaseGates"]]
    assert len(gate_ids) == len(set(gate_ids))
    referenced_gates = {
        gate
        for section in ("profiles", "wireVersions")
        for entry in matrix[section]
        for gate in entry.get("requiredGates", [])
    }
    referenced_gates.update(
        gate
        for entry in matrix["releaseGates"]
        for gate in entry.get("companionGates", [])
    )
    referenced_gates.update(entry["promotionGate"] for entry in integration_entries)
    referenced_gates.update(entry["promotionGate"] for entry in matrix["profiles"])
    referenced_gates.update(
        entry["promotionGate"]
        for entry in matrix["artifacts"]
        if "promotionGate" in entry
    )
    referenced_gates.update(matrix["globalRequiredGates"])
    assert referenced_gates == set(gate_ids)

    operator_gate = next(
        entry
        for entry in matrix["releaseGates"]
        if entry["id"] == "REL-KUBERNETES-OPERATOR"
    )
    assert operator_gate["readiness"] == "definition-blocked"
    assert operator_gate["scope"] == "artifact-promotion"
    assert operator_gate["blocksTargetRelease"] is False
    assert operator_gate["artifact"] == deployment_chart["id"]
    assert operator_gate["currentMaturity"] == "scaffold-only"
    assert operator_gate["requiredEvidence"] == [
        "controller-source-and-signed-oci-image",
        "watch-reconcile-status-convergence",
        "idempotent-reconcile-and-conflict-retry",
        "finalizer-deletion-lifecycle",
        "leader-election-and-failover",
        "envtest-reconciliation",
        "kind-install-and-upgrade",
        "crd-version-migration-and-rollback",
    ]
    assert operator_gate["blockers"] == [
        "controller-implementation-absent",
        "controller-image-absent",
        "reconciliation-lifecycle-evidence-absent",
    ]
    assert operator_gate["id"] not in matrix["globalRequiredGates"]
    assert all(
        operator_gate["id"] not in profile["requiredGates"]
        for profile in matrix["profiles"]
    )
    assert all((ROOT / path).is_file() for path in operator_gate["currentEvidence"])

    audit = matrix["deepAudit"]
    assert audit["releaseBlockingSeverities"] == ["P0", "P1"]
    assert audit["defectScope"] == "all-code-shipped-in-release-artifacts"
    assert audit["baseline"]["total"] == sum(audit["baseline"]["bySeverity"].values()) == 99
    coverage_path = ROOT / audit["coverageMap"]
    assert coverage_path.is_file()
    coverage = yaml.safe_load(coverage_path.read_text(encoding="utf-8"))
    assert coverage["auditDate"] == audit["auditDate"]
    assert coverage["artifactDigests"] == audit["artifactDigests"]

    baseline_by_severity = coverage["baselineBySeverity"]
    assert set(baseline_by_severity) == set(audit["baseline"]["bySeverity"])
    baseline_ids: list[str] = []
    for severity, findings in baseline_by_severity.items():
        assert len(findings) == audit["baseline"]["bySeverity"][severity]
        baseline_ids.extend(findings)
    assert len(baseline_ids) == audit["baseline"]["total"]
    assert all(re.fullmatch(r"GB-[A-Z]+-\d{3}", finding) for finding in baseline_ids)
    assert all(count == 1 for count in Counter(baseline_ids).values())

    workstreams = coverage["workstreams"]
    assert len({entry["id"] for entry in workstreams}) == len(workstreams)
    mapped_findings = [
        finding for workstream in workstreams for finding in workstream["findings"]
    ]
    assert Counter(mapped_findings) == Counter({finding: 1 for finding in baseline_ids})
    assert all(
        set(workstream["affectedProfiles"]) <= catalog_profiles
        for workstream in workstreams
    )
    assert all(
        set(workstream["releaseGates"]) <= set(gate_ids)
        for workstream in workstreams
    )

    reproduced = [entry["id"] for entry in coverage["reproducedFindings"]]
    assert len(reproduced) == audit["baseline"]["reproduced"] == len(set(reproduced))
    assert set(reproduced) <= set(baseline_ids)
    assert (ROOT / coverage["reproductionManifest"]).is_file()
    assert (ROOT / coverage["reproductionChecker"]).is_file()

    audit_gate = next(
        entry for entry in matrix["releaseGates"] if entry["id"] == "REL-AUDIT-REMEDIATION"
    )
    assert audit_gate["readiness"] == (
        "code-closure-enforced-external-evidence-blocked"
    )
    assert audit["liveStatus"] == {
        "authority": "docs/project/audit-issue-status.yaml",
        "inventory": "docs/project/audit-issues.json",
        "checker": "tools/check_audit_inventory.py",
        "resolved": 57,
        "openBySeverity": {"P0": 0, "P1": 0, "P2": 34, "P3": 8},
    }
    assert "signed-candidate-and-final-promotion-audit-closure-binding" in (
        audit_gate["implementedEvidence"]
    )
    assert "exact-cli-string-list-and-object-codecs-with-type-confusion-regressions" in (
        audit_gate["implementedEvidence"]
    )
    assert "append-friendly-server-histories-with-constant-time-byte-accounting" in (
        audit_gate["implementedEvidence"]
    )
    assert "cold-root-and-canonical-import-time-rss-and-module-budgets" in (
        audit_gate["implementedEvidence"]
    )
    assert "tenant-owner-indexed-run-list-cursor-pagination-with-page-cap" in (
        audit_gate["implementedEvidence"]
    )
    assert "fourteen-production-module-strict-mypy-and-no-new-ignore-budget" in (
        audit_gate["implementedEvidence"]
    )
    assert audit_gate["blockers"] == [
        "audited-source-commit-tree-or-archive-digest-unavailable",
    ]
    assert audit_gate["exitCriteria"]["maxOpenBySeverity"] == {"P0": 0, "P1": 0}
    assert audit_gate["companionGates"] == [
        "REL-OBJECT-AUTHORIZATION-REVIEW",
        "REL-ADVERSARIAL-RESOURCE-TESTS",
        "REL-MACOS-NATIVE-SMOKE",
    ]

    macos_gate = next(
        entry for entry in matrix["releaseGates"] if entry["id"] == "REL-MACOS-NATIVE-SMOKE"
    )
    assert macos_gate["classification"] == "smoke-only"
    assert macos_gate["changesSupportedPlatformMatrix"] is False

    authority = matrix["authorityTransition"]
    assert authority["readiness"] == "in-progress-blocked"
    assert authority["decisionStatus"] == "accepted"
    assert authority["currentCandidateImplementation"] == "phase-scoped-rust-authority"
    assert authority["targetNormativeAuthority"] == "rust"
    assert authority["publicCompilerAuthority"] == "rust"
    assert authority["pythonRole"] == "authoring-facade-and-explicit-reference-oracle"
    assert authority["implicitReferenceFallback"] is False
    assert "production-scheduler-and-durable-authority" in authority[
        "remainingPhases"
    ]
    assert "supported-installed-native-compiler-tck-and-artifact-evidence" not in (
        authority["remainingPhases"]
    )
    assert "supported-installed-native-compiler-tck-and-artifact-evidence" in (
        authority["completedPhases"]
    )
    assert "control-plane-library-extraction" not in authority["remainingPhases"]
    assert "control-plane-library-extraction" in authority["completedPhases"]
    assert "runtime-protocol-capability-handshake" not in authority[
        "remainingPhases"
    ]
    assert "runtime-protocol-capability-handshake" in authority["completedPhases"]
    assert authority["blocksTargetRelease"] is True
    assert authority["requiredGate"] == "REL-NORMATIVE-AUTHORITY"
    authority_decision = ROOT / authority["decision"]
    assert authority_decision.is_file()
    assert "Status: Accepted" in authority_decision.read_text(encoding="utf-8")

    matrix_profiles = {entry["id"]: entry for entry in matrix["profiles"]}
    assert matrix_profiles["GB-C0-SCHEMA"]["implementation"] == (
        "rust-native-compiler+python-facade"
    )
    assert "REL-NORMATIVE-AUTHORITY" in matrix_profiles["GB-C0-SCHEMA"][
        "requiredGates"
    ]
    assert matrix_profiles["GB-C1-LOCAL-RUNTIME"]["implementation"] == (
        "rust-native-runtime-target+python-reference-oracle"
    )
    assert matrix_profiles["GB-C1-LOCAL-RUNTIME"]["authority"] == {
        "activeCompiler": "rust",
        "activeReferenceInterpreter": "python",
        "targetProductionScheduler": "rust-transition-blocked",
        "inheritedAuthorityFrom": "extends",
    }
    assert "REL-NORMATIVE-AUTHORITY" in matrix_profiles["GB-C1-LOCAL-RUNTIME"][
        "requiredGates"
    ]
    for profile_id in (
        "GB-C2-AI-APPLICATION",
        "GB-C3-GOVERNED-RUNTIME",
        "GB-C4-PRODUCTION",
        "GB-X1-ORCHESTRATION",
        "GB-X2-VOICE",
        "GB-X3-DURABLE-STREAM",
    ):
        profile = matrix_profiles[profile_id]
        assert "implementation" not in profile
        assert profile["extensionImplementation"] == "python-reference"
        assert profile["inheritsCoreAuthority"] is True
    assert artifacts["pypi:graphblocks"]["stableClaimRequires"] == [
        "pypi:graphblocks-runtime"
    ]
    assert artifacts["pypi:graphblocks-runtime"]["tier"] == "stable"
    assert artifacts["pypi:graphblocks-runtime"]["readiness"] == (
        "authority-transition-blocked"
    )

    authority_gate = next(
        entry
        for entry in matrix["releaseGates"]
        if entry["id"] == "REL-NORMATIVE-AUTHORITY"
    )
    assert authority_gate["decisionStatus"] == "accepted"
    assert authority_gate["decision"] == authority["decision"]
    assert set(authority_gate["completedEvidence"]) <= set(
        authority_gate["requiredEvidence"]
    )
    assert (
        "installed-native-compiler-tck-differential-and-artifact-identity"
        in authority_gate["completedEvidence"]
    )
    assert "authority-transition-adr-not-accepted" not in authority_gate["blockers"]
    assert "production-runtime-authority-transition-incomplete" in authority_gate[
        "blockers"
    ]
    assert "installed-native-compiler-tck-and-artifact-identity-incomplete" not in (
        authority_gate["blockers"]
    )
    assert "control-plane-dependency-inversion-incomplete" not in authority_gate[
        "blockers"
    ]
    assert "python-binding-depends-on-control-plane-library-not-daemon" in (
        authority_gate["completedEvidence"]
    )
    assert "protocol-capability-and-unsupported-version-handshake" in (
        authority_gate["completedEvidence"]
    )
    assert (
        "protocol-capability-and-unsupported-version-handshake-incomplete"
        not in authority_gate["blockers"]
    )

    traceability = yaml.safe_load(
        (ROOT / "docs" / "project" / "stable-requirements.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert traceability["authorityDecision"] == authority["decision"]
    assert traceability["authorityRoles"] == {
        "graphCompiler": "rust",
        "productionRuntimeTarget": "rust",
        "python": "authoring-facade-and-explicit-reference-oracle",
        "implicitReferenceFallback": False,
    }

    api_gate = next(entry for entry in matrix["releaseGates"] if entry["id"] == "REL-API-SNAPSHOT")
    assert api_gate["readiness"] == "candidate-enforced"
    assert set(api_gate["blockers"]) == {"compatibility-review"}
    for evidence_path in api_gate["evidence"]:
        assert (ROOT / evidence_path).is_file(), f"missing API snapshot evidence: {evidence_path}"


def test_numeric_diagnostic_codes_have_unique_registry_entries() -> None:
    registry = yaml.safe_load(
        (ROOT / "docs" / "specification" / "reference" / "diagnostic-codes.yaml").read_text()
    )
    assert registry["registryVersion"] == 1
    pattern = re.compile(registry["codePattern"])
    status_values = set(registry["statusValues"])
    tier_values = set(registry["tierValues"])

    entries = registry["codes"]
    registered = [entry["code"] for entry in entries]
    assert len(registered) == len(set(registered))
    for entry in entries:
        assert pattern.fullmatch(entry["code"])
        assert entry["status"] in status_values
        assert entry["tier"] in tier_values
        assert entry["defaultSeverity"] in {"error", "warning", "info"}
        assert entry["meaning"].strip()

    emitted: set[str] = set()
    for root, suffix in (
        (ROOT / "src", "*.py"),
        (ROOT / "packages", "*.py"),
        (ROOT / "crates", "*.rs"),
    ):
        for source in root.rglob(suffix):
            emitted.update(re.findall(r"\bGB\d{4}\b", source.read_text(encoding="utf-8")))
    assert emitted == set(registered)

    stable_entries = [entry for entry in entries if entry["tier"] == "stable"]
    assert stable_entries
    assert all(entry["status"] == "active" for entry in stable_entries)
