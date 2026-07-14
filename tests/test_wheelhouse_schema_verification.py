from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace

import pytest

from graphblocks.schema import SchemaManifest


def _load_wheelhouse_module() -> ModuleType:
    module_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("verify_wheelhouse_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_content_digest(module: ModuleType, payload: dict[str, object]) -> dict[str, object]:
    payload = dict(payload)
    payload["contentDigest"] = module.canonical_hash(payload)
    return payload


def _write_mock_sdist(module: ModuleType, *, source_root: Path, output_root: Path) -> Path:
    project_bytes = (source_root / "pyproject.toml").read_bytes()
    project = module.tomllib.loads(project_bytes.decode("utf-8"))["project"]
    normalized_name = str(project["name"]).replace("-", "_").replace(".", "_")
    archive_root = f"{normalized_name}-{project['version']}"
    destination = output_root / f"{archive_root}.tar.gz"
    with tarfile.open(destination, "w:gz") as archive:
        root_info = tarfile.TarInfo(archive_root)
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        archive.addfile(root_info)
        manifest_info = tarfile.TarInfo(f"{archive_root}/pyproject.toml")
        manifest_info.size = len(project_bytes)
        manifest_info.mode = 0o644
        archive.addfile(manifest_info, io.BytesIO(project_bytes))
    return destination


def test_release_evidence_gate_requires_nonempty_identity_bound_tck_reports() -> None:
    module = _load_wheelhouse_module()
    digest = "sha256:" + "a" * 64
    valid = _with_content_digest(module, {
        "ok": True,
        "reports": {
            "schema": {
                "ok": True,
                "evidence": {
                    "fixture_digest": digest,
                    "implementation": "graphblocks-python",
                    "implementation_version": "0.1.0",
                    "suite": "schema",
                },
                "results": [{"case_id": "schema-1", "status": "passed"}],
            }
        },
    })

    assert module._require_release_evidence(valid, kind="TCK") == valid

    invalid = dict(valid)
    invalid["reports"] = {"schema": {"ok": True, "evidence": {}, "results": []}}
    with pytest.raises(RuntimeError, match="contains no executed cases"):
        module._require_release_evidence(invalid, kind="TCK")


def test_release_evidence_gate_requires_executed_acceptance_applications() -> None:
    module = _load_wheelhouse_module()
    digest = "sha256:" + "b" * 64
    application = {
        "application_id": "app-1",
        "scenario_path": "acceptance/app-1.yaml",
        "application_digest": digest,
        "scenario_digest": digest,
        "ok": True,
        "results": [
            {
                "application_id": "app-1",
                "gate": "validate",
                "status": "passed",
                "output_digest": digest,
            }
        ],
    }
    valid = _with_content_digest(module, {
        "ok": True,
        "manifest_digest": digest,
        "applications": [application],
    })
    expected = {
        "manifest_digest": digest,
        "applications": {
            "app-1": {
                "application_digest": digest,
                "scenario_path": "acceptance/app-1.yaml",
                "scenario_digest": digest,
                "gates": ("validate",),
            }
        },
    }

    assert module._require_release_evidence(
        valid,
        kind="acceptance",
        expected_acceptance=expected,
    ) == valid

    invalid = dict(valid)
    invalid_application = dict(valid["applications"][0])
    invalid_application["results"] = []
    invalid["applications"] = [invalid_application]
    with pytest.raises(RuntimeError, match="contains no executed gates"):
        module._require_release_evidence(
            invalid,
            kind="acceptance",
            expected_acceptance=expected,
        )


def test_release_evidence_gate_recomputes_content_digest() -> None:
    module = _load_wheelhouse_module()
    fixture_digest = "sha256:" + "a" * 64
    payload = _with_content_digest(module, {
        "ok": True,
        "reports": {
            "schema": {
                "ok": True,
                "evidence": {
                    "fixture_digest": fixture_digest,
                    "implementation": "graphblocks-python",
                    "implementation_version": "0.1.0",
                    "suite": "schema",
                },
                "results": [{"case_id": "schema-1", "status": "passed"}],
            }
        },
    })
    payload["profile"] = "tampered-after-digest"

    with pytest.raises(RuntimeError, match="does not match its content"):
        module._require_release_evidence(payload, kind="TCK")


def test_installed_tck_evidence_preserves_arbitrary_precision_json_numbers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    fixture_digest = "sha256:" + "a" * 64
    payload = {
        "ok": True,
        "reports": {
            "schema": {
                "ok": True,
                "evidence": {
                    "fixture_digest": fixture_digest,
                    "implementation": "graphblocks-python",
                    "implementation_version": "0.1.0",
                    "suite": "schema",
                },
                "results": [
                    {
                        "case_id": "arbitrary-precision",
                        "status": "passed",
                        "observed": {"value": Decimal("1e400")},
                    }
                ],
            }
        },
    }
    payload["contentDigest"] = module.canonical_hash(payload)
    stdout = module.canonical_dumps(payload)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
        ),
    )

    observed = module._run_json_command(
        ["graphblocks-tck", "run-all"],
        cwd=tmp_path,
        env={},
        kind="TCK",
    )

    assert observed["reports"]["schema"]["results"][0]["observed"][
        "value"
    ] == Decimal("1e400")


def test_acceptance_evidence_gate_rejects_self_reported_subset() -> None:
    module = _load_wheelhouse_module()
    digest = "sha256:" + "b" * 64
    application = {
        "application_id": "app-1",
        "scenario_path": "acceptance/app-1.yaml",
        "application_digest": digest,
        "scenario_digest": digest,
        "ok": True,
        "results": [
            {
                "application_id": "app-1",
                "gate": "validate",
                "status": "passed",
                "output_digest": digest,
            }
        ],
    }
    payload = _with_content_digest(module, {
        "ok": True,
        "manifest_digest": digest,
        "applications": [application],
    })
    expected_application = {
        "application_digest": digest,
        "scenario_path": "acceptance/app-1.yaml",
        "scenario_digest": digest,
        "gates": ("validate",),
    }

    with pytest.raises(RuntimeError, match="does not cover every application"):
        module._require_release_evidence(
            payload,
            kind="acceptance",
            expected_acceptance={
                "manifest_digest": digest,
                "applications": {
                    "app-1": expected_application,
                    "app-2": expected_application,
                },
            },
        )


def test_checked_in_acceptance_expectations_bind_manifest_scenarios_and_gates() -> None:
    module = _load_wheelhouse_module()
    expectations = module._acceptance_expectations(
        module.ROOT / "acceptance" / "applications.yaml",
        root=module.ROOT,
    )

    assert str(expectations["manifest_digest"]).startswith("sha256:")
    applications = expectations["applications"]
    assert len(applications) == 10
    assert applications["kubernetes-canary"]["gates"] == (
        "graphblocks validate",
        "release bundle verification",
        "canary quality gate",
        "rollback and drain gate",
    )
    assert str(applications["kubernetes-canary"]["scenario_digest"]).startswith("sha256:")


def test_stable_tck_expectations_bind_bundled_c0_c1_profiles_and_contract_digests() -> None:
    module = _load_wheelhouse_module()
    expectations = module.release_evidence_expectations(module.ROOT)["TCK"]

    assert expectations["claimed_profiles"] == (
        "GB-C0-SCHEMA",
        "GB-C1-LOCAL-RUNTIME",
    )
    assert set(expectations["suites"]) == {
        "application-events",
        "compiler",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
    }
    assert str(expectations["schema_manifest_digest"]).startswith("sha256:")
    assert str(expectations["profile_catalog_digest"]).startswith("sha256:")


def test_sbom_gate_requires_pinned_generator_and_first_party_coverage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=module.CYCLONEDX_BOM_VERSION,
            )
        output = Path(command[command.index("--output-file") + 1])
        output.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.6",
                    "components": [
                        {
                            "name": "GraphBlocks_Testing",
                            "version": "1.0.0",
                            "bom-ref": "graphblocks-testing==1.0.0",
                        },
                        {
                            "name": "pip",
                            "version": "25.1.1",
                            "bom-ref": "pip==25.1.1",
                        },
                        {
                            "name": "setuptools",
                            "version": "80.9.0",
                            "bom-ref": "setuptools==80.9.0",
                        },
                    ],
                    "metadata": {
                        "component": {
                            "name": "GraphBlocks",
                            "version": "1.0.0",
                            "bom-ref": "root-component",
                        },
                        "tools": {
                            "components": [
                                {"name": "cyclonedx-py", "version": "7.3.0"}
                            ]
                        },
                    },
                    "dependencies": [
                        {
                            "ref": "graphblocks-testing==1.0.0",
                            "dependsOn": [],
                        },
                        {"ref": "pip==25.1.1", "dependsOn": []},
                        {"ref": "root-component", "dependsOn": []},
                        {
                            "ref": "setuptools==80.9.0",
                            "dependsOn": ["pip==25.1.1"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    output = tmp_path / "sbom.cdx.json"
    module._generate_cyclonedx_sbom(
        python_environment=tmp_path / "venv" / "bin" / "python",
        output_path=output,
        expected_distributions={"graphblocks": "1.0.0", "graphblocks-testing": "1.0.0"},
        expected_artifacts={
            "graphblocks-1.0.0-py3-none-any.whl": {
                "filename": "graphblocks-1.0.0-py3-none-any.whl",
                "sha256": "a" * 64,
                    "distribution": "graphblocks",
                    "version": "1.0.0",
                    "artifactType": "wheel",
            },
            "graphblocks_testing-1.0.0-py3-none-any.whl": {
                "filename": "graphblocks_testing-1.0.0-py3-none-any.whl",
                "sha256": "b" * 64,
                    "distribution": "graphblocks-testing",
                    "version": "1.0.0",
                    "artifactType": "wheel",
            },
        },
    )

    assert output.is_file()
    assert commands[0][-1] == "--version"
    assert "--output-reproducible" in commands[1]
    assert commands[1][commands[1].index("--sv") + 1] == "1.6"
    payload = json.loads(output.read_text(encoding="utf-8"))
    release_components = [
        component
        for component in payload["components"]
        if any(
            prop == {"name": "graphblocks:release-artifact", "value": "true"}
            for prop in component.get("properties", [])
        )
    ]
    assert {component["name"] for component in release_components} == {
        "graphblocks-1.0.0-py3-none-any.whl",
        "graphblocks_testing-1.0.0-py3-none-any.whl",
    }
    assert {
        component["hashes"][0]["content"] for component in release_components
    } == {"a" * 64, "b" * 64}
    assert {component["name"] for component in payload["components"]} == {
        "GraphBlocks_Testing",
        "graphblocks-1.0.0-py3-none-any.whl",
        "graphblocks_testing-1.0.0-py3-none-any.whl",
    }
    assert {relationship["ref"] for relationship in payload["dependencies"]} == {
        "graphblocks-testing==1.0.0",
        "root-component",
    }
    assert payload["metadata"]["tools"]["components"] == [
        {"name": "cyclonedx-py", "version": "7.3.0"}
    ]


def test_sbom_gate_rejects_unpinned_generator_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="7.2.2",
        ),
    )

    with pytest.raises(RuntimeError, match="requires cyclonedx-bom==7.3.0"):
        module._generate_cyclonedx_sbom(
            python_environment=tmp_path / "python",
            output_path=tmp_path / "sbom.json",
            expected_distributions={"graphblocks": "1.0.0"},
            expected_artifacts={},
        )


def test_sdist_extraction_rejects_traversal_links_and_filename_manifest_mismatch(
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()

    def write_archive(name: str, members: list[tarfile.TarInfo]) -> Path:
        archive_path = tmp_path / name
        with tarfile.open(archive_path, "w:gz") as archive:
            for member in members:
                content = (
                    b'[project]\nname = "graphblocks"\nversion = "1.0.0"\n'
                    if member.isfile()
                    else None
                )
                if content is not None:
                    member.size = len(content)
                archive.addfile(member, io.BytesIO(content) if content is not None else None)
        return archive_path

    root = tarfile.TarInfo("graphblocks-1.0.0")
    root.type = tarfile.DIRTYPE
    traversal = tarfile.TarInfo("graphblocks-1.0.0/../outside")
    traversal.size = 1
    with pytest.raises(RuntimeError, match="escapes"):
        module._safe_extract_sdist(
            write_archive("graphblocks-1.0.0.tar.gz", [root, traversal]),
            tmp_path / "traversal",
        )

    ads_root = tarfile.TarInfo("graphblocks-1.0.0")
    ads_root.type = tarfile.DIRTYPE
    alternate_stream = tarfile.TarInfo("graphblocks-1.0.0/file:stream")
    with pytest.raises(RuntimeError, match="escapes"):
        module._safe_extract_sdist(
            write_archive(
                "graphblocks-1.0.0.tar.gz", [ads_root, alternate_stream]
            ),
            tmp_path / "alternate-stream",
        )

    link_root = tarfile.TarInfo("graphblocks-1.0.0")
    link_root.type = tarfile.DIRTYPE
    link = tarfile.TarInfo("graphblocks-1.0.0/pyproject.toml")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    with pytest.raises(RuntimeError, match="link or special"):
        module._safe_extract_sdist(
            write_archive("graphblocks-1.0.0.tar.gz", [link_root, link]),
            tmp_path / "link",
        )

    mismatch_root = tarfile.TarInfo("graphblocks_testing-1.0.0")
    mismatch_root.type = tarfile.DIRTYPE
    mismatch_manifest = tarfile.TarInfo(
        "graphblocks_testing-1.0.0/pyproject.toml"
    )
    with pytest.raises(RuntimeError, match="name/version"):
        module._safe_extract_sdist(
            write_archive(
                "graphblocks_testing-1.0.0.tar.gz",
                [mismatch_root, mismatch_manifest],
            ),
            tmp_path / "mismatch",
        )


@pytest.mark.parametrize(
    "invalid_digest",
    (
        "sha256:short",
        "sha256:" + "A" * 64,
        "sha512:" + "a" * 64,
    ),
)
def test_release_evidence_gate_rejects_noncanonical_digests(invalid_digest: str) -> None:
    module = _load_wheelhouse_module()

    with pytest.raises(RuntimeError, match="canonical sha256"):
        module._require_release_evidence(
            {
                "ok": True,
                "contentDigest": invalid_digest,
                "reports": {
                    "schema": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "a" * 64,
                            "implementation": "graphblocks-python",
                            "implementation_version": "0.1.0",
                            "suite": "schema",
                        },
                        "results": [
                            {"case_id": "schema-1", "status": "passed"}
                        ],
                    }
                },
            },
            kind="TCK",
        )


@pytest.mark.parametrize("installed_output_kind", ("incomplete", "malformed"))
def test_wheelhouse_gate_rejects_invalid_installed_schema_manifest(
    monkeypatch,
    tmp_path,
    installed_output_kind: str,
) -> None:
    module = _load_wheelhouse_module()

    root = tmp_path / "repo"
    for manifest_path, distribution in (
        (root / "pyproject.toml", "graphblocks"),
        (
            root / "packages" / "graphblocks-runtime" / "pyproject.toml",
            "graphblocks-runtime",
        ),
        (
            root / "packages" / "graphblocks-testing" / "pyproject.toml",
            "graphblocks-testing",
        ),
    ):
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            f'[project]\nname = "{distribution}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
    schema_root = root / "schemas"
    schema_root.mkdir()
    for name in ("first", "second"):
        (schema_root / f"{name}.schema.json").write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": f"example.com/{name}.schema.json",
                    "title": name.title(),
                    "type": "object",
                }
            ),
            encoding="utf-8",
        )
    subset_root = tmp_path / "installed-schemas"
    subset_root.mkdir()
    (subset_root / "first.schema.json").write_text(
        (schema_root / "first.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    installed_payload = SchemaManifest.from_directory(subset_root).manifest_payload()
    installed_output = (
        json.dumps(installed_payload)
        if installed_output_kind == "incomplete"
        else "{not-json"
    )

    class FakeEnvBuilder:
        def __init__(self, *, with_pip: bool) -> None:
            assert with_pip

        def create(self, path: str) -> None:
            (Path(path) / "bin").mkdir(parents=True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["rustc", "--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="rustc 1.94.0 (012345678 2026-01-01)\n",
            )
        if "build" in command and "--outdir" in command:
            output_root = Path(command[command.index("--outdir") + 1])
            manifest_root = Path(command[-1])
            if "--sdist" in command:
                _write_mock_sdist(
                    module,
                    source_root=manifest_root,
                    output_root=output_root,
                )
            else:
                project = module.tomllib.loads(
                    (manifest_root / "pyproject.toml").read_text(encoding="utf-8")
                )["project"]
                wheel_name = str(project["name"]).replace("-", "_")
                (output_root / f"{wheel_name}-0.1.0-py3-none-any.whl").write_bytes(
                    b"wheel"
                )
        if command[-4:] == ["-m", "graphblocks", "schemas", "manifest"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=installed_output,
            )
        if command[-3:] == ["pip", "list", "--format=json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    [
                        {"name": "graphblocks", "version": "0.1.0"},
                        {"name": "graphblocks-runtime", "version": "0.1.0"},
                        {"name": "graphblocks-testing", "version": "0.1.0"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(
        module,
        "build_wheel_matrix",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            targets=(
                SimpleNamespace(manifest="pyproject.toml"),
                SimpleNamespace(manifest="packages/graphblocks-runtime/pyproject.toml"),
                SimpleNamespace(manifest="packages/graphblocks-testing/pyproject.toml"),
            ),
            diagnostics=(),
        ),
    )
    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="installed schema manifest"):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])


def test_wheelhouse_gate_uses_pep503_distribution_identity(monkeypatch, tmp_path) -> None:
    module = _load_wheelhouse_module()
    expected_schema = SchemaManifest.from_directory(module.ROOT / "schemas").manifest_payload()
    root_version = module.tomllib.loads(
        (module.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    runtime_version = module.tomllib.loads(
        (module.ROOT / "packages" / "graphblocks-runtime" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]["version"]
    testing_version = module.tomllib.loads(
        (module.ROOT / "packages" / "graphblocks-testing" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]["version"]
    wheel_source_roots: list[Path] = []

    class FakeEnvBuilder:
        def __init__(self, *, with_pip: bool) -> None:
            assert with_pip

        def create(self, path: str) -> None:
            (Path(path) / "bin").mkdir(parents=True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["rustc", "--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="rustc 1.94.0 (012345678 2026-01-01)\n",
            )
        if "build" in command and "--outdir" in command:
            output_root = Path(command[command.index("--outdir") + 1])
            manifest_root = Path(command[-1])
            if "--sdist" in command:
                _write_mock_sdist(
                    module,
                    source_root=manifest_root,
                    output_root=output_root,
                )
            else:
                wheel_source_roots.append(manifest_root)
                project = module.tomllib.loads(
                    (manifest_root / "pyproject.toml").read_text(encoding="utf-8")
                )["project"]
                wheel_name = str(project["name"]).replace("-", "_")
                (output_root / f"{wheel_name}-{project['version']}-py3-none-any.whl").write_bytes(
                    b"wheel"
                )
        if "download" in command and "--dest" in command:
            dependency_root = Path(command[command.index("--dest") + 1])
            (dependency_root / "jsonschema-4.25.1-py3-none-any.whl").write_bytes(
                b"dependency"
            )
        if command[-4:] == ["-m", "graphblocks", "schemas", "manifest"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(expected_schema))
        if command[-3:] == ["pip", "list", "--format=json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                        [
                            {"name": "GraphBlocks", "version": root_version},
                            {"name": "GraphBlocks_Runtime", "version": runtime_version},
                            {"name": "GraphBlocks.Testing", "version": testing_version},
                            {"name": "jsonschema", "version": "4.25.1"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(module.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "_run_json_command",
        lambda command, *, kind, **kwargs: _with_content_digest(
            module,
            {"ok": True, "kind": kind},
        ),
    )

    generated_closures: list[dict[str, str]] = []

    def fake_generate_sbom(
        *,
        output_path: Path,
        expected_distributions: Mapping[str, str],
        **kwargs: object,
    ) -> None:
        generated_closures.append(dict(expected_distributions))
        output_path.write_text(
            json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(module, "_generate_cyclonedx_sbom", fake_generate_sbom)
    wheelhouse = tmp_path / "wheelhouse"
    sdist_root = tmp_path / "sdists"
    dependency_wheelhouse = tmp_path / "dependencies"
    evidence = tmp_path / "evidence"
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert module.main(
        [
            "--wheelhouse",
            str(wheelhouse),
            "--sdist-dir",
            str(sdist_root),
            "--dependency-wheelhouse",
            str(dependency_wheelhouse),
            "--release-evidence-dir",
            str(evidence),
            "--sbom-output",
            str(evidence / "sbom.cdx.json"),
            "--platform",
            "ubuntu-latest",
            "--python-version",
            python_version,
        ]
    ) == 0
    assert {path.name for path in wheelhouse.glob("*.whl")} == {
        f"graphblocks-{root_version}-py3-none-any.whl",
        f"graphblocks_runtime-{runtime_version}-py3-none-any.whl",
        f"graphblocks_testing-{testing_version}-py3-none-any.whl",
    }
    assert {path.name for path in sdist_root.glob("*.tar.gz")} == {
        f"graphblocks-{root_version}.tar.gz",
        f"graphblocks_runtime-{runtime_version}.tar.gz",
        f"graphblocks_testing-{testing_version}.tar.gz",
    }
    assert len(wheel_source_roots) == 3
    assert all("graphblocks-sdist-extract-" in str(path) for path in wheel_source_roots)
    assert {path.name for path in dependency_wheelhouse.glob("*.whl")} == {
        "jsonschema-4.25.1-py3-none-any.whl"
    }
    assert {path.name for path in evidence.iterdir()} == {
        "acceptance.json",
        "platform.json",
        "sbom.cdx.json",
        "tck.json",
    }
    standalone_sbom = tmp_path / "standalone-sbom.cdx.json"
    assert module.main(
        [
            "--wheelhouse",
            str(tmp_path / "standalone-wheelhouse"),
            "--sdist-dir",
            str(tmp_path / "standalone-sdists"),
            "--dependency-wheelhouse",
            str(tmp_path / "standalone-dependencies"),
            "--sbom-output",
            str(standalone_sbom),
        ]
    ) == 0
    assert standalone_sbom.is_file()
    assert len(generated_closures) == 2
    assert all(
        closure.get("jsonschema") == "4.25.1" for closure in generated_closures
    )


def test_wheelhouse_gate_derives_build_targets_from_package_catalog(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_wheelhouse_module()
    root = tmp_path / "repo"
    manifest = root / "custom" / "pyproject.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[project]\nname = "custom-wheel"\nversion = "0.1.0"\n', encoding="utf-8")
    catalog = {"catalogVersion": 1}
    matrix = SimpleNamespace(
        ok=True,
        targets=(SimpleNamespace(manifest="custom/pyproject.toml"),),
        diagnostics=(),
    )
    matrix_calls: list[tuple[Path, object]] = []

    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "load_package_catalog", lambda: catalog, raising=False)

    def fake_build_wheel_matrix(path: Path, *, catalog: object) -> object:
        matrix_calls.append((path, catalog))
        return matrix

    monkeypatch.setattr(module, "build_wheel_matrix", fake_build_wheel_matrix, raising=False)

    class ExpectedStop(Exception):
        pass

    def stop_after_first_build(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | None:
        if command == ["rustc", "--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="rustc 1.94.0 (012345678 2026-01-01)\n",
            )
        assert Path(command[-1]) == manifest.parent
        raise ExpectedStop

    monkeypatch.setattr(module.subprocess, "run", stop_after_first_build)

    with pytest.raises(ExpectedStop):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])
    assert matrix_calls == [(root, catalog)]
