from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace
import zipfile

import pytest

from graphblocks._version import __version__ as GRAPHBLOCKS_VERSION
from graphblocks.canonical import (
    canonical_dumps_reference,
    canonical_hash_reference,
)
from graphblocks.schema import SchemaManifest
from tools.verify_wheelhouse import (
    installed_native_authority_probe_expectations,
    stable_runtime_api_snapshot,
    stable_runtime_smoke_expectation,
)


def _load_wheelhouse_module() -> ModuleType:
    module_path = Path(__file__).parents[1] / "tools" / "verify_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("verify_wheelhouse_schema", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheelhouse_expectations_use_reference_canonical_oracle() -> None:
    module = _load_wheelhouse_module()

    assert module.canonical_dumps is canonical_dumps_reference
    assert module.canonical_hash is canonical_hash_reference


def _with_content_digest(module: ModuleType, payload: dict[str, object]) -> dict[str, object]:
    payload = dict(payload)
    payload["contentDigest"] = module.canonical_hash(payload)
    return payload


def _patch_json_process(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    returncode: int,
    stdout: str,
    stderr: str = "",
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(module.subprocess, "run", run)


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


def _write_mock_wheel(
    module: ModuleType,
    *,
    source_root: Path,
    output_root: Path,
) -> Path:
    project = module.tomllib.loads(
        (source_root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    wheel_name = str(project["name"]).replace("-", "_")
    destination = output_root / f"{wheel_name}-{project['version']}-py3-none-any.whl"
    members = [(f"{wheel_name}/__init__.py", b"")]
    if module.canonicalize_name(str(project["name"])) == "graphblocks-runtime":
        members.append(
            ("graphblocks_runtime/_native.abi3.so", b"mock-native-extension")
        )
    with zipfile.ZipFile(destination, "w") as archive:
        for name, content in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return destination


def _write_host_system_wheel(
    destination: Path,
    *,
    create_system: int,
) -> dict[str, bytes]:
    members = {
        "graphblocks/__init__.py": b'__version__ = "1.0.0rc12"\n',
        "graphblocks-1.0.0rc12.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: graphblocks\nVersion: 1.0.0rc12\n"
        ),
        "graphblocks-1.0.0rc12.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nTag: py3-none-any\n"
        ),
        "graphblocks-1.0.0rc12.dist-info/RECORD": b"",
    }
    with zipfile.ZipFile(destination, "w") as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = create_system
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return members


def _native_runtime_persistence_payload(
    native_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    process = {
        "artifact": dict(native_artifact) if native_artifact is not None else {
            "distributionVersion": "0.1.0",
            "filename": "_native.abi3.so",
            "sha256": "1" * 64,
            "size": 123,
        },
        "contract": {
            "runId": "installed-native-runtime-reopen",
            "graphHash": "sha256:" + "2" * 64,
            "inputs": {"message": {"text": "ok"}},
            "status": "completed",
            "stateRevision": 0,
            "terminalKind": "run_succeeded",
            "journalKinds": [
                "run_started",
                "node_started",
                "node_completed",
                "run_succeeded",
            ],
            "journalSequences": [1, 2, 3, 4],
        },
        "referencePackageImported": False,
    }
    fence = deepcopy(process)
    fence["contract"] = {
        "runId": "run-000001",
        "firstLease": {
            "owner": "coordinator-a",
            "leaseId": "run-000001:1",
            "fencingEpoch": 1,
            "acquiredAtUnixMs": 1_000,
            "expiresAtUnixMs": 1_500,
        },
        "secondLease": {
            "owner": "coordinator-b",
            "leaseId": "run-000001:2",
            "fencingEpoch": 2,
            "acquiredAtUnixMs": 1_501,
            "expiresAtUnixMs": 2_000,
        },
        "stalePatch": {
            "accepted": False,
            "errorCode": "run_ownership_lease_mismatch",
            "expectedLease": {
                "owner": "coordinator-b",
                "leaseId": "run-000001:2",
                "fencingEpoch": 2,
            },
            "actualLease": {
                "owner": "coordinator-a",
                "leaseId": "run-000001:1",
                "fencingEpoch": 1,
            },
        },
        "staleStatus": {
            "accepted": False,
            "attemptedStatus": "failed",
            "errorCode": "run_ownership_lease_mismatch",
            "expectedLease": {
                "owner": "coordinator-b",
                "leaseId": "run-000001:2",
                "fencingEpoch": 2,
            },
            "actualLease": {
                "owner": "coordinator-a",
                "leaseId": "run-000001:1",
                "fencingEpoch": 1,
            },
        },
        "afterStaleAttempts": {
            "state": {},
            "stateRevision": 0,
            "status": "created",
        },
        "authoritativePatch": {
            "accepted": True,
            "state": {"owner": "coordinator-b"},
            "stateRevision": 1,
            "status": "created",
        },
        "authoritativeStatus": {
            "accepted": True,
            "stateRevision": 1,
            "status": "running",
        },
        "reopened": {
            "state": {"owner": "coordinator-b"},
            "stateRevision": 1,
            "status": "running",
        },
    }
    return {
        "formatVersion": 2,
        "writer": deepcopy(process),
        "reader": deepcopy(process),
        "fence": fence,
    }


def _native_binding_payload(
    *,
    distribution_version: str = "0.1.0",
    binding_version: str = "0.1.0",
    protocol_version: object = 1,
    capabilities: object = (
        "canonical.json.v1",
        "compiler.graph.v1",
        "protocol.application.v1",
        "protocol.worker.v1",
        "runtime.local.v1",
        "schema.identity.v1",
        "schema.resource-migration.v1",
        "schema.resource-validation.v1",
    ),
    native_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "canonicalSmoke": {
            "hash": "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
            "json": '{"a":1,"b":2}',
        },
        "distributionVersion": distribution_version,
        "publicFacadeEvidence": installed_native_authority_probe_expectations(),
        "runtimePersistence": _native_runtime_persistence_payload(native_artifact),
        "schemaIdSmoke": {
            "canonical": "schemas/Message@4294967295",
            "majorVersion": 4_294_967_295,
            "name": "schemas/Message",
        },
        "stableRuntimeApi": stable_runtime_api_snapshot(),
        "stableRuntimeSmoke": stable_runtime_smoke_expectation(),
        "status": {
            "available": True,
            "binding_crate": "graphblocks-python",
            "binding_version": binding_version,
            "binding_protocol_version": protocol_version,
            "capabilities": list(capabilities),
            "module": "graphblocks_runtime._native",
            "error": None,
        },
    }


def test_release_json_writer_bypasses_text_newline_translation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()

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
    output = tmp_path / "evidence.json"

    module._write_utf8_lf(output, '{"message":"stable"}\n')

    assert output.read_bytes() == b'{"message":"stable"}\n'


def test_installed_native_binding_handshake_requires_versioned_capabilities() -> None:
    module = _load_wheelhouse_module()
    payload = _native_binding_payload(
        capabilities=(
            "canonical.json.v1",
            "compiler.graph.v1",
            "protocol.application.v1",
            "protocol.worker.v1",
            "runtime.local.v1",
            "schema.identity.v1",
            "schema.resource-migration.v1",
            "schema.resource-validation.v1",
            "vendor.future.v1",
        )
    )

    assert module._validate_installed_native_binding(
        payload,
        expected_distribution_version="0.1.0",
    ) == payload["status"]


def test_installed_native_binding_handshake_rejects_wrong_canonical_smoke() -> None:
    module = _load_wheelhouse_module()
    payload = _native_binding_payload()
    payload["canonicalSmoke"] = {
        "hash": "sha256:" + "0" * 64,
        "json": '{"b":2,"a":1}',
    }

    with pytest.raises(RuntimeError, match="canonical smoke does not match"):
        module._validate_installed_native_binding(
            payload,
            expected_distribution_version="0.1.0",
        )


def test_installed_native_binding_handshake_rejects_wrong_schema_id_smoke() -> None:
    module = _load_wheelhouse_module()
    payload = _native_binding_payload()
    payload["schemaIdSmoke"] = {
        "canonical": "schemas/Message@1",
        "majorVersion": 1,
        "name": "schemas/Message",
    }

    with pytest.raises(RuntimeError, match="schema id smoke does not match"):
        module._validate_installed_native_binding(
            payload,
            expected_distribution_version="0.1.0",
        )


@pytest.mark.parametrize(
    "target",
    ("signature", "status-fields", "runtime-smoke"),
)
def test_installed_native_binding_rejects_wrong_stable_runtime_api(
    target: str,
) -> None:
    module = _load_wheelhouse_module()
    payload = _native_binding_payload()
    if target == "signature":
        runtime_api = deepcopy(payload["stableRuntimeApi"])
        runtime_api["symbols"][-1]["signature"] = "(graph: object) -> object"
        payload["stableRuntimeApi"] = runtime_api
        message = "stable runtime API snapshot"
    elif target == "status-fields":
        runtime_api = deepcopy(payload["stableRuntimeApi"])
        runtime_api["nativeExtensionStatusFields"] = ["available"]
        payload["stableRuntimeApi"] = runtime_api
        message = "stable runtime API snapshot"
    else:
        runtime_smoke = deepcopy(payload["stableRuntimeSmoke"])
        runtime_smoke["outputs"] = {"prompt": "wrong"}
        payload["stableRuntimeSmoke"] = runtime_smoke
        message = "stable runtime API smoke"

    with pytest.raises(RuntimeError, match=message):
        module._validate_installed_native_binding(
            payload,
            expected_distribution_version="0.1.0",
        )


@pytest.mark.parametrize(
    "target",
    ("corpus", "resource-validation", "resource-migration"),
)
def test_installed_native_binding_rejects_wrong_public_facade_evidence(
    target: str,
) -> None:
    module = _load_wheelhouse_module()
    payload = _native_binding_payload()
    evidence = deepcopy(payload["publicFacadeEvidence"])
    if target == "corpus":
        evidence["corpusDigest"] = "sha256:" + "0" * 64
    elif target == "resource-validation":
        evidence["resourceValidationCases"][0]["public"]["valid"] = False
    else:
        evidence["resourceMigrationCases"][0]["native"]["ok"] = False
    payload["publicFacadeEvidence"] = evidence

    with pytest.raises(RuntimeError, match="public/native authority facade"):
        module._validate_installed_native_binding(
            payload,
            expected_distribution_version="0.1.0",
        )


def test_installed_native_authority_expectations_cover_full_shared_corpora() -> None:
    module = _load_wheelhouse_module()
    evidence = module.installed_native_authority_probe_expectations()

    assert evidence["formatVersion"] == 2
    assert len(evidence["resourceValidationCases"]) == 20
    assert len(evidence["resourceMigrationCases"]) == 7
    for owner in ("resourceValidationCases", "resourceMigrationCases"):
        for case in evidence[owner]:
            assert case["public"] == case["reference"] == case["native"]


def test_installed_native_authority_evidence_binds_runtime_artifact() -> None:
    module = _load_wheelhouse_module()
    runtime_artifact = {
        "filename": "graphblocks_runtime-0.1.0-cp311-abi3-manylinux.whl",
        "sha256": "1" * 64,
        "size": 123,
        "distribution": "graphblocks-runtime",
        "version": "0.1.0",
        "artifactType": "wheel",
    }
    runtime_extension_artifact = deepcopy(
        _native_runtime_persistence_payload()["writer"]["artifact"]
    )
    evidence = {
        "runtimeArtifact": dict(runtime_artifact),
        "runtimeExtensionArtifact": dict(runtime_extension_artifact),
        "probe": _native_binding_payload(),
    }

    assert module.validate_installed_native_authority_evidence(
        evidence,
        expected_runtime_artifact=runtime_artifact,
        expected_runtime_extension_artifact=runtime_extension_artifact,
    ) == evidence

    tampered = dict(evidence)
    tampered["runtimeArtifact"] = {
        **runtime_artifact,
        "sha256": "2" * 64,
    }
    with pytest.raises(RuntimeError, match="another runtime artifact"):
        module.validate_installed_native_authority_evidence(
            tampered,
            expected_runtime_artifact=runtime_artifact,
            expected_runtime_extension_artifact=runtime_extension_artifact,
        )

    tampered_extension = deepcopy(evidence)
    tampered_extension["runtimeExtensionArtifact"]["sha256"] = "3" * 64
    with pytest.raises(RuntimeError, match="another runtime extension"):
        module.validate_installed_native_authority_evidence(
            tampered_extension,
            expected_runtime_artifact=runtime_artifact,
            expected_runtime_extension_artifact=runtime_extension_artifact,
        )


def test_native_runtime_wheel_member_artifact_hashes_exact_extension_bytes() -> None:
    module = _load_wheelhouse_module()
    extension = b"exact-native-extension-bytes"
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("graphblocks_runtime/__init__.py", b"")
        archive.writestr("graphblocks_runtime/_native.abi3.so", extension)

    assert module.native_runtime_wheel_member_artifact(
        wheel.getvalue(),
        distribution_version="0.1.0",
    ) == {
        "filename": "_native.abi3.so",
        "sha256": hashlib.sha256(extension).hexdigest(),
        "size": len(extension),
        "distributionVersion": "0.1.0",
    }


def test_private_build_directory_fails_closed_on_collision_and_cleans_up(
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    build_root = tmp_path / ".graphblocks-sdist-extract"
    build_root.mkdir()
    marker = build_root / "keep"
    marker.write_text("existing", encoding="utf-8")

    with pytest.raises(RuntimeError, match="already exists"):
        with module._private_build_directory(build_root):
            pytest.fail("an existing private build directory must not be reused")
    assert marker.read_text(encoding="utf-8") == "existing"

    marker.unlink()
    build_root.rmdir()
    with pytest.raises(ValueError, match="build failed"):
        with module._private_build_directory(build_root) as created_root:
            assert created_root == build_root
            raise ValueError("build failed")
    assert not build_root.exists()


def test_platform_independent_wheel_host_system_normalization_is_cross_platform(
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    unix_wheel = tmp_path / "unix" / "graphblocks-1.0.0rc12-py3-none-any.whl"
    windows_wheel = tmp_path / "windows" / "graphblocks-1.0.0rc12-py3-none-any.whl"
    unix_wheel.parent.mkdir()
    windows_wheel.parent.mkdir()
    members = _write_host_system_wheel(unix_wheel, create_system=3)
    assert members == _write_host_system_wheel(windows_wheel, create_system=0)
    assert unix_wheel.read_bytes() != windows_wheel.read_bytes()

    assert module.normalize_platform_independent_wheel_host_system(unix_wheel) is False
    assert module.normalize_platform_independent_wheel_host_system(windows_wheel) is True

    assert unix_wheel.read_bytes() == windows_wheel.read_bytes()
    with zipfile.ZipFile(windows_wheel) as archive:
        assert archive.testzip() is None
        assert {info.create_system for info in archive.infolist()} == {3}
        assert {info.filename: archive.read(info) for info in archive.infolist()} == members


def test_platform_specific_wheel_host_system_is_not_rewritten(tmp_path: Path) -> None:
    module = _load_wheelhouse_module()
    wheel = tmp_path / "graphblocks-1.0.0rc12-cp311-abi3-win_amd64.whl"
    _write_host_system_wheel(wheel, create_system=0)
    original = wheel.read_bytes()

    assert module.normalize_platform_independent_wheel_host_system(wheel) is False
    assert wheel.read_bytes() == original


def test_platform_independent_wheel_host_system_normalization_fails_closed(
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    wheel = tmp_path / "graphblocks-1.0.0rc12-py3-none-any.whl"
    _write_host_system_wheel(wheel, create_system=0)
    malformed = bytearray(wheel.read_bytes())
    malformed[-18:-16] = (1).to_bytes(2, "little")
    wheel.write_bytes(malformed)

    with pytest.raises(RuntimeError, match="unsupported ZIP directory layout"):
        module.normalize_platform_independent_wheel_host_system(wheel)


def test_installed_native_authority_probe_exercises_public_and_reference_paths() -> None:
    module = _load_wheelhouse_module()
    source = module.installed_native_authority_probe_source()

    assert "canonical_loads(source)" in source
    assert "canonical_loads_reference(source)" in source
    assert "SchemaId.parse(source)" in source
    assert "SchemaId.parse_reference(source)" in source
    assert "graphblocks_runtime.parse_schema_id(source)" in source
    assert "resource_schema_errors(deepcopy(document))" in source
    assert "resource_schema_errors_reference(deepcopy(document))" in source
    assert "graphblocks_runtime.resource_schema_errors(deepcopy(document))" in source
    assert "migration_contract(migrate_document, document)" in source
    assert "migration_contract(migrate_document_reference, document)" in source
    assert "native_migration_contract(document)" in source
    assert "graphblocks_runtime.require_native_extension()" in source
    assert "inspect.signature(value)" in source
    assert "graphblocks_runtime.run_stdlib_graph(" in source
    compile(source, "<installed-native-authority-probe>", "exec")


def test_installed_native_runtime_reopen_probe_uses_three_fresh_native_processes(
) -> None:
    module = _load_wheelhouse_module()
    writer_source = module.installed_native_runtime_reopen_writer_source()
    reader_source = module.installed_native_runtime_reopen_reader_source()
    fence_source = module.installed_native_runtime_fence_source()
    authority_source = module.installed_native_authority_probe_source()

    assert "graphblocks_runtime.run_stdlib_graph_with_options(" in writer_source
    assert "graphblocks_runtime._inspect_runtime_evidence(" in reader_source
    assert "graphblocks_runtime._evaluate_runtime_fence_reopen(" in fence_source
    assert "'graphblocks' in sys.modules" in writer_source
    assert "'graphblocks' in sys.modules" in reader_source
    assert "writer_process = subprocess.run(" in authority_source
    assert "reader_process = subprocess.run(" in authority_source
    assert "fence_process = subprocess.run(" in authority_source
    compile(writer_source, "<installed-native-runtime-writer>", "exec")
    compile(reader_source, "<installed-native-runtime-reader>", "exec")
    compile(fence_source, "<installed-native-runtime-fence>", "exec")
    compile(authority_source, "<installed-native-authority-probe>", "exec")


@pytest.mark.parametrize(
    "target",
    ("artifact", "contract", "fence", "reference-import", "sequence", "envelope"),
)
def test_installed_native_runtime_reopen_evidence_fails_closed(target: str) -> None:
    module = _load_wheelhouse_module()
    payload = _native_runtime_persistence_payload()
    if target == "artifact":
        payload["reader"]["artifact"]["sha256"] = "3" * 64
        message = "different native artifacts"
    elif target == "contract":
        payload["reader"]["contract"]["stateRevision"] = 3
        message = "differs from the writer contract"
    elif target == "fence":
        payload["fence"]["contract"]["stalePatch"]["accepted"] = True
        message = "fence contract is invalid"
    elif target == "reference-import":
        payload["reader"]["referencePackageImported"] = True
        message = "imported the reference package"
    elif target == "sequence":
        for role in ("writer", "reader"):
            payload[role]["contract"]["journalSequences"] = [1, 2, 4, 5]
        message = "reopen contract is invalid"
    else:
        payload["unexpected"] = True
        message = "evidence is not closed"

    with pytest.raises(RuntimeError, match=message):
        module.validate_installed_native_runtime_reopen_evidence(
            payload,
            expected_distribution_version="0.1.0",
        )


def test_installed_native_runtime_reopen_evidence_accepts_exact_readback() -> None:
    module = _load_wheelhouse_module()
    payload = _native_runtime_persistence_payload()

    assert (
        module.validate_installed_native_runtime_reopen_evidence(
            payload,
            expected_distribution_version="0.1.0",
        )
        == payload
    )


def test_installed_native_runtime_reopen_evidence_rejects_other_wheel_member() -> None:
    module = _load_wheelhouse_module()
    payload = _native_runtime_persistence_payload()
    selected_member = deepcopy(payload["writer"]["artifact"])
    selected_member["sha256"] = "f" * 64

    with pytest.raises(RuntimeError, match="does not match the selected wheel member"):
        module.validate_installed_native_runtime_reopen_evidence(
            payload,
            expected_distribution_version="0.1.0",
            expected_native_artifact=selected_member,
        )


def test_wheelhouse_runs_native_authority_probe_from_a_script_file() -> None:
    module = _load_wheelhouse_module()
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert 'Path(install_root) / "native-authority-probe.py"' in source
    assert "installed_native_authority_probe_source() + \"\\n\"" in source
    assert 'str(native_authority_probe),' in source


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            _native_binding_payload(protocol_version=True),
            "unsupported protocol version",
        ),
        (
            _native_binding_payload(
                capabilities=(
                    "protocol.application.v1",
                    "protocol.worker.v1",
                )
            ),
            "missing required capabilities",
        ),
        (
            _native_binding_payload(
                capabilities=(
                    "canonical.json.v1",
                    "compiler.graph.v1",
                    "protocol.application.v1",
                    "protocol.worker.v1",
                    "schema.identity.v1",
                    "schema.resource-migration.v1",
                    "schema.resource-validation.v1",
                )
            ),
            "missing required capabilities: runtime.local.v1",
        ),
        (
            _native_binding_payload(binding_version="0.2.0"),
            "implementation version does not match",
        ),
        (
            _native_binding_payload(capabilities=({},)),
            "invalid capabilities",
        ),
        (
            _native_binding_payload(distribution_version="0.2.0"),
            "distribution version does not match",
        ),
    ),
)
def test_installed_native_binding_handshake_rejects_incompatible_artifacts(
    payload: dict[str, object],
    message: str,
) -> None:
    module = _load_wheelhouse_module()

    with pytest.raises(RuntimeError, match=message):
        module._validate_installed_native_binding(
            payload,
            expected_distribution_version="0.1.0",
        )


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


def test_release_evidence_binds_native_reports_to_exact_runtime_wheel() -> None:
    module = _load_wheelhouse_module()
    artifact = {
        "filename": "graphblocks_runtime-0.1.0-cp311-abi3-linux_x86_64.whl",
        "sha256": "a" * 64,
        "size": 1024,
        "distribution": "graphblocks-runtime",
        "version": "0.1.0",
        "artifactType": "wheel",
    }

    def payload(observed_artifact: object) -> dict[str, object]:
        return _with_content_digest(
            module,
            {
                "ok": True,
                "reports": {
                    "application-events": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "d" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "application-events",
                        },
                        "results": [
                            {
                                "case_id": "application-events/native-stream",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"updates": []},
                                    "reference_contract": {"updates": []},
                                    "native_tck_reference_match": True,
                                    "native_tck_contract": {
                                        "diagnostics": [],
                                        "observed": {
                                            "accepted_events": [],
                                            "operation_results": [
                                                {"operationIndex": 0}
                                            ],
                                        },
                                    },
                                    "reference_tck_contract": {
                                        "diagnostics": [],
                                        "observed": {
                                            "accepted_events": [],
                                            "operation_results": [
                                                {"operationIndex": 0}
                                            ],
                                        },
                                    },
                                },
                            }
                        ],
                    },
                    "compiler": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "b" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "compiler",
                        },
                        "results": [
                            {"case_id": "compiler/native", "status": "passed"}
                        ],
                    },
                    "retry": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "e" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "retry",
                        },
                        "results": [
                            {
                                "case_id": "retry/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"attempts": 1},
                                    "reference_contract": {"attempts": 1},
                                },
                            }
                        ],
                    },
                    "sequence": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "f" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "sequence",
                        },
                        "results": [
                            {
                                "case_id": "sequence/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"state": "open"},
                                    "reference_contract": {"state": "open"},
                                },
                            }
                        ],
                    },
                    "tool-execution": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "1" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "tool-execution",
                        },
                        "results": [
                            {
                                "case_id": "tool-execution/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"states": {}},
                                    "reference_contract": {"states": {}},
                                },
                            }
                        ],
                    },
                    "tool-lifecycle": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "2" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "tool-lifecycle",
                        },
                        "results": [
                            {
                                "case_id": "tool-lifecycle/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"admitted": False},
                                    "reference_contract": {"admitted": False},
                                },
                            }
                        ],
                    },
                    "tool-result": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "3" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "tool-result",
                        },
                        "results": [
                            {
                                "case_id": "tool-result/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"ok": True},
                                    "reference_contract": {"ok": True},
                                },
                            }
                        ],
                    },
                    "typed-ports": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "4" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "typed-ports",
                            "execution_claim": {
                                "executor_id": "rust-typed-ports-exact-differential",
                                "implementation": "graphblocks-runtime",
                                "language": "rust",
                                "comparison": "exact-native-reference",
                                "reference_implementation": "graphblocks-python",
                            },
                        },
                        "results": [
                            {
                                "case_id": "typed-ports/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"ok": True},
                                    "reference_contract": {"ok": True},
                                },
                            }
                        ],
                    },
                    "outcome": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "5" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "outcome",
                            "execution_claim": {
                                "executor_id": "rust-outcome-exact-differential",
                                "implementation": "graphblocks-runtime",
                                "language": "rust",
                                "comparison": "exact-native-reference",
                                "reference_implementation": "graphblocks-python",
                            },
                        },
                        "results": [
                            {
                                "case_id": "outcome/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                    "native_contract": {"ok": True},
                                    "reference_contract": {"ok": True},
                                },
                            }
                        ],
                    },
                    "runtime": {
                        "ok": True,
                        "evidence": {
                            "fixture_digest": "sha256:" + "c" * 64,
                            "implementation": "graphblocks-runtime",
                            "implementation_version": "0.1.0",
                            "implementation_artifact": observed_artifact,
                            "suite": "runtime",
                        },
                        "results": [
                            {
                                "case_id": "runtime/native",
                                "status": "passed",
                                "observed": {
                                    "runtime": "native",
                                    "native_reference_match": True,
                                },
                            }
                        ],
                    },
                },
            },
        )

    valid = payload(dict(artifact))
    assert module._require_release_evidence(
        valid,
        kind="TCK",
        expected_compiler_artifact=artifact,
    ) == valid

    mismatched = payload(dict(artifact))
    mismatched["reports"]["typed-ports"]["results"][0]["observed"][
        "native_contract"
    ] = {"ok": False}
    mismatched.pop("contentDigest")
    mismatched = _with_content_digest(module, mismatched)
    with pytest.raises(RuntimeError, match="typed-ports TCK evidence is not exact"):
        module._require_release_evidence(
            mismatched,
            kind="TCK",
            expected_compiler_artifact=artifact,
        )

    mismatched_outcome = payload(dict(artifact))
    mismatched_outcome["reports"]["outcome"]["results"][0]["observed"][
        "native_contract"
    ] = {"ok": False}
    mismatched_outcome.pop("contentDigest")
    mismatched_outcome = _with_content_digest(module, mismatched_outcome)
    with pytest.raises(RuntimeError, match="outcome TCK evidence is not exact"):
        module._require_release_evidence(
            mismatched_outcome,
            kind="TCK",
            expected_compiler_artifact=artifact,
        )

    substituted = dict(artifact)
    substituted["sha256"] = "c" * 64
    with pytest.raises(RuntimeError, match="exact graphblocks-runtime wheel"):
        module._require_release_evidence(
            payload(substituted),
            kind="TCK",
            expected_compiler_artifact=artifact,
        )

    with pytest.raises(RuntimeError, match="exact graphblocks-runtime wheel"):
        module._require_release_evidence(
            payload(None),
            kind="TCK",
            expected_compiler_artifact=artifact,
        )
    with pytest.raises(RuntimeError, match="require an exact native artifact"):
        module._require_release_evidence(
            valid,
            kind="TCK",
            expected_tck={
                "suites": {
                    "compiler": {
                        "implementation_artifact_distribution": (
                            "graphblocks-runtime"
                        )
                    }
                }
            },
        )


def test_release_tck_expectations_validate_observed_suite_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    expected_digest = "sha256:" + "a" * 64
    expected = {
        "manifest_digest": expected_digest,
        "claimed_profiles": ("GB-C0-SCHEMA",),
        "authority_claim": {"matrix_digest": expected_digest},
        "profile_catalog_digest": expected_digest,
        "schema_manifest_digest": expected_digest,
        "suites": {
            "schema": {
                "case_ids": ("schema-1",),
                "case_ids_digest": module.canonical_hash(
                    {"case_ids": ["schema-1"]}
                ),
                "fixture_digest": expected_digest,
                "implementation": "graphblocks-python",
                "implementation_version": "1.0.0rc12",
                "suite_manifest_digest": expected_digest,
                "execution_claim": {
                    "executor_id": "python-reference",
                    "implementation": "graphblocks-python",
                    "language": "python",
                    "comparison": "reference-only",
                    "reference_implementation": "graphblocks-python",
                },
            }
        },
    }

    def observed_payload(fixture_digest: str) -> dict[str, object]:
        return _with_content_digest(
            module,
            {
                "profile": "local",
                "ok": True,
                "suite_manifest_digest": expected_digest,
                "claimed_profiles": ["GB-C0-SCHEMA"],
                "authority_claim": {"matrix_digest": expected_digest},
                "profile_catalog_digest": expected_digest,
                "schema_manifest_digest": expected_digest,
                "reports": {
                    "schema": {
                        "ok": True,
                        "evidence": {
                            "case_ids_digest": expected["suites"]["schema"][
                                "case_ids_digest"
                            ],
                            "fixture_digest": fixture_digest,
                            "implementation": "graphblocks-python",
                            "implementation_version": "1.0.0rc12",
                            "execution_claim": expected["suites"]["schema"][
                                "execution_claim"
                            ],
                            "suite": "schema",
                            "suite_manifest_digest": expected_digest,
                        },
                        "results": [
                            {"case_id": "schema-1", "status": "passed"}
                        ],
                    }
                },
            },
        )

    valid = observed_payload(expected_digest)
    assert module._require_release_evidence(
        valid,
        kind="TCK",
        expected_tck=expected,
    ) == valid

    missing_authority = dict(valid)
    missing_authority.pop("authority_claim")
    with pytest.raises(RuntimeError, match="authority matrix claim"):
        module._require_release_evidence(
            missing_authority,
            kind="TCK",
            expected_tck=expected,
        )

    wrong_execution = observed_payload(expected_digest)
    wrong_execution["reports"]["schema"]["evidence"]["execution_claim"] = {
        **expected["suites"]["schema"]["execution_claim"],
        "comparison": "exact-native-reference",
    }
    with pytest.raises(RuntimeError, match="execution_claim"):
        module._require_release_evidence(
            wrong_execution,
            kind="TCK",
            expected_tck=expected,
        )

    with pytest.raises(RuntimeError, match="fixture_digest"):
        module._require_release_evidence(
            observed_payload("sha256:" + "b" * 64),
            kind="TCK",
            expected_tck=expected,
        )

    conflicting = observed_payload(expected_digest)
    conflicting["reports"]["schema"]["evidence"]["case_ids_digest"] = (
        "sha256:" + "b" * 64
    )
    conflicting["contentDigest"] = module.canonical_hash(
        {key: value for key, value in conflicting.items() if key != "contentDigest"}
    )
    _patch_json_process(
        monkeypatch,
        module,
        returncode=0,
        stdout=module.canonical_dumps(conflicting),
    )

    with pytest.raises(RuntimeError, match="case_ids_digest"):
        module._run_json_command(
            ["graphblocks-tck", "run-all"],
            cwd=tmp_path,
            env={},
            kind="TCK",
            expected_tck=expected,
        )


def test_run_json_command_reports_bounded_failed_tck_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    payload = {
        "ok": False,
        "reports": {
            "compiler": {
                "ok": False,
                "results": [
                    {
                        "case_id": (
                            "\x1b[31mwindows\ncase-00"
                            if index == 0
                            else f"windows-case-{index:02d}"
                        ),
                        "status": "failed",
                    }
                    for index in range(22)
                ],
            }
        },
    }

    _patch_json_process(
        monkeypatch,
        module,
        returncode=1,
        stdout=module.canonical_dumps(payload),
    )

    with pytest.raises(RuntimeError) as captured:
        module._run_json_command(
            ["graphblocks-tck", "run-all"],
            cwd=tmp_path,
            env={},
            kind="TCK",
        )

    message = str(captured.value)
    assert "compiler/?[31mwindows?case-00" in message
    assert "compiler/windows-case-19" in message
    assert "windows-case-20" not in message
    assert "\x1b" not in message
    assert "\n" not in message
    assert message.endswith("additional failures omitted")


def test_run_json_command_preserves_bounded_non_json_failure_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    _patch_json_process(
        monkeypatch,
        module,
        returncode=7,
        stdout="",
        stderr=(
            "\x1b[31mchild\nfailure\x00 "
            + ("head-detail " * 100)
            + "omitted-middle-marker "
            + ("tail-detail " * 200)
            + "final ValueError: incompatible wheel"
        ),
    )

    with pytest.raises(RuntimeError) as captured:
        module._run_json_command(
            ["graphblocks-tck", "run-all"],
            cwd=tmp_path,
            env={},
            kind="TCK",
        )

    message = str(captured.value)
    assert "exited with status 7 without valid JSON" in message
    assert "?[31mchild failure?" in message
    assert "output omitted" in message
    assert "omitted-middle-marker" not in message
    assert message.endswith("final ValueError: incompatible wheel")
    assert "\x1b" not in message
    assert "\n" not in message
    assert len(message) < 2_100


def test_run_json_command_rejects_nonzero_exit_with_passing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    payload = _with_content_digest(
        module,
        {
            "ok": True,
            "reports": {
                "schema": {
                    "ok": True,
                    "evidence": {
                        "fixture_digest": "sha256:" + "a" * 64,
                        "implementation": "graphblocks-python",
                        "implementation_version": "0.1.0",
                        "suite": "schema",
                    },
                    "results": [{"case_id": "schema-1", "status": "passed"}],
                }
            },
        },
    )
    _patch_json_process(
        monkeypatch,
        module,
        returncode=9,
        stdout=module.canonical_dumps(payload),
    )

    with pytest.raises(RuntimeError, match="status 9 despite passing evidence"):
        module._run_json_command(
            ["graphblocks-tck", "run-all"],
            cwd=tmp_path,
            env={},
            kind="TCK",
        )


def test_default_tck_output_matches_source_derived_release_expectations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(
        str(root / "packages" / "graphblocks-testing" / "src")
    )
    graphblocks_testing = importlib.import_module("graphblocks_testing")
    module = _load_wheelhouse_module()
    wheel = tmp_path / "graphblocks_runtime-0.1.0-cp311-abi3-linux_x86_64.whl"
    wheel.write_bytes(b"release compiler wheel")
    compiler_artifact = {
        "filename": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "size": wheel.stat().st_size,
        "distribution": "graphblocks-runtime",
        "version": "0.1.0",
        "artifactType": "wheel",
    }
    reference_compile = graphblocks_testing.compile_graph
    monkeypatch.setattr(
        graphblocks_testing,
        "_native_compiler_wheel_artifact",
        lambda path: dict(compiler_artifact),
    )
    monkeypatch.setattr(
        graphblocks_testing,
        "_compile_graph_normative",
        reference_compile,
    )
    monkeypatch.setattr(
        graphblocks_testing,
        "_native_compiler_version",
        lambda: "0.1.0",
    )
    graphblocks_runtime = importlib.import_module("graphblocks_runtime")
    application_event_module = importlib.import_module(
        "graphblocks.application_event"
    )
    testing_cli = importlib.import_module("graphblocks_testing.cli")
    runtime_module = importlib.import_module("graphblocks.runtime")
    outcome_reference_module = importlib.import_module(
        "graphblocks._outcome_reference"
    )

    def reference_runtime_bridge(
        graph: Mapping[str, object],
        inputs: Mapping[str, object],
        **options: object,
    ) -> dict[str, object]:
        run_id = str(options["run_id"])
        result = runtime_module.LocalRuntime(
            testing_cli._tck_registry("runtime")
        ).run(graph, inputs, run_id)
        return {
            "runId": result.run_id,
            "status": result.status,
            "outputs": runtime_module._mutable_json_like(result.outputs),
            "journal": [
                {"kind": record.kind} for record in result.journal.records
            ],
        }

    def reference_application_event_stream_bridge(
        state: dict[str, object],
        operations: object,
    ) -> dict[str, object]:
        assert state == {"acceptedEvents": []}
        assert isinstance(operations, list)
        stream = application_event_module.ApplicationEventStreamState()
        accepted_wires_by_id: dict[str, dict[str, object]] = {}
        updates: list[dict[str, object]] = []
        for operation_index, operation in enumerate(operations):
            assert isinstance(operation, dict) and operation["kind"] == "event"
            raw_event = operation["event"]
            assert isinstance(raw_event, dict)
            raw_metadata = raw_event["metadata"]
            assert isinstance(raw_metadata, dict)
            occurred_at_unix_ms = raw_metadata["occurredAtUnixMs"]
            assert isinstance(occurred_at_unix_ms, int)
            metadata = application_event_module.ApplicationEventMetadata(
                event_id=str(raw_metadata["eventId"]),
                run_id=str(raw_metadata["runId"]),
                response_id=str(raw_metadata["responseId"]),
                turn_id=raw_metadata["turnId"],
                cursor=raw_metadata["cursor"],
                graph_id=raw_metadata["graphId"],
                node_id=raw_metadata["nodeId"],
                operation_id=raw_metadata["operationId"],
                sequence=raw_metadata["sequence"],
                release_id=str(raw_metadata["releaseId"]),
                policy_snapshot_id=str(raw_metadata["policySnapshotId"]),
                occurred_at=datetime.fromtimestamp(
                    occurred_at_unix_ms / 1000,
                    timezone.utc,
                )
                .isoformat()
                .replace("+00:00", "Z"),
                visibility=raw_metadata["visibility"],
            )
            payload = raw_event["payload"]
            assert isinstance(payload, dict)
            if raw_event["kind"] == "OutputCutoff":
                cutoff_occurred_at_unix_ms = payload["occurred_at_unix_ms"]
                assert isinstance(cutoff_occurred_at_unix_ms, int)
                payload = dict(payload)
                payload.pop("occurred_at_unix_ms")
                payload["occurred_at"] = (
                    datetime.fromtimestamp(
                        cutoff_occurred_at_unix_ms / 1000,
                        timezone.utc,
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            event = application_event_module.ApplicationEvent(
                kind=raw_event["kind"],
                metadata=metadata,
                payload=payload,
                tool_call_id=raw_event["toolCallId"],
            )
            accepted = stream.accept(event)
            if accepted is None:
                updates.append(
                    {
                        "operationIndex": operation_index,
                        "kind": "dropped",
                        "event": raw_event,
                    }
                )
                continue
            accepted_wires_by_id.setdefault(accepted.metadata.event_id, raw_event)
            updates.append(
                {
                    "operationIndex": operation_index,
                    "kind": "accepted",
                    "event": accepted_wires_by_id[accepted.metadata.event_id],
                }
            )
        return {
            "ok": True,
            "updates": updates,
            "state": {
                "acceptedEvents": [
                    accepted_wires_by_id[event.metadata.event_id]
                    for event in stream.accepted_events
                ],
                "cutoffResponses": sorted(stream.cutoffs),
            },
        }

    def reference_application_event_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        raw_operations = raw_case["operations"]
        raw_expected_kinds = raw_case["expectedAcceptedKinds"]
        raw_expected_diagnostics = raw_case.get("expectedDiagnostics", [])
        assert isinstance(raw_operations, list)
        assert isinstance(raw_expected_kinds, list)
        assert isinstance(raw_expected_diagnostics, list)
        case = graphblocks_testing.TckCase.application_events(
            case_id=str(raw_case["name"]),
            operations=tuple(dict(operation) for operation in raw_operations),
            expected_accepted_kinds=tuple(str(kind) for kind in raw_expected_kinds),
            expected_diagnostics=tuple(
                dict(diagnostic) for diagnostic in raw_expected_diagnostics
            ),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_tck_contract = result.observed["reference_tck_contract"]
        assert isinstance(reference_tck_contract, dict)
        return json.loads(json.dumps(reference_tck_contract))

    def reference_retry_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        case = graphblocks_testing.TckCase.retry(
            case_id=str(raw_case["name"]),
            fixture=dict(raw_case),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    def reference_sequence_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        raw_operations = raw_case.get("operations", [])
        raw_expected = raw_case["expected"]
        assert isinstance(raw_operations, list)
        assert isinstance(raw_expected, dict)
        case = graphblocks_testing.TckCase.sequence(
            case_id=str(raw_case["name"]),
            capacity=int(raw_case["capacity"]),
            operations=tuple(dict(operation) for operation in raw_operations),
            expected_state=raw_expected.get("state"),
            expected_creation_error=raw_expected.get("creation_error"),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    def reference_tool_execution_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        case = graphblocks_testing.TckCase.tool_execution(
            case_id=str(raw_case["name"]),
            fixture=dict(raw_case),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    def reference_tool_lifecycle_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        case = graphblocks_testing.TckCase.tool_lifecycle(
            case_id=str(raw_case["name"]),
            fixture=dict(raw_case),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    def reference_tool_result_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        case = graphblocks_testing.TckCase.tool_result(
            case_id=str(raw_case["name"]),
            fixture=dict(raw_case),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    def reference_typed_ports_tck_case_bridge(
        raw_case: dict[str, object],
    ) -> dict[str, object]:
        case = graphblocks_testing.TckCase.typed_ports(
            case_id=str(raw_case["name"]),
            fixture=dict(raw_case),
        )
        result = graphblocks_testing.TckRunner(
            graphblocks_testing.stdlib_registry()
        ).run_cases((case,)).results[0]
        assert result.status == "passed"
        reference_contract = result.observed["reference_contract"]
        assert isinstance(reference_contract, dict)
        return json.loads(json.dumps(reference_contract))

    monkeypatch.setattr(
        graphblocks_runtime,
        "run_stdlib_graph",
        reference_runtime_bridge,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "evaluate_application_event_stream",
        reference_application_event_stream_bridge,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_application_event_tck_case",
        reference_application_event_tck_case_bridge,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_retry_tck_case",
        reference_retry_tck_case_bridge,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_sequence_tck_case",
        reference_sequence_tck_case_bridge,
        raising=False,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_tool_execution_tck_case",
        reference_tool_execution_tck_case_bridge,
        raising=False,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_tool_lifecycle_tck_case",
        reference_tool_lifecycle_tck_case_bridge,
        raising=False,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_tool_result_tck_case",
        reference_tool_result_tck_case_bridge,
        raising=False,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_typed_ports_tck_case",
        reference_typed_ports_tck_case_bridge,
        raising=False,
    )
    monkeypatch.setattr(
        graphblocks_runtime,
        "_evaluate_outcome_tck_case",
        outcome_reference_module.evaluate_outcome_tck_case_reference,
        raising=False,
    )

    exit_code = graphblocks_testing.main(
        ["run-all", "--native-compiler-wheel", str(wheel), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out, parse_float=Decimal)
    expected = module.release_evidence_expectations(root)["TCK"]
    assert module._require_release_evidence(
        payload,
        kind="TCK",
        expected_tck=expected,
        expected_compiler_artifact=compiler_artifact,
    ) == payload


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
    _patch_json_process(
        monkeypatch,
        module,
        returncode=0,
        stdout=stdout,
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
        "outcome",
        "retry",
        "runtime",
        "schema",
        "sequence",
        "tool-execution",
        "tool-lifecycle",
        "tool-result",
        "typed-ports",
    }
    assert str(expectations["schema_manifest_digest"]).startswith("sha256:")
    assert str(expectations["profile_catalog_digest"]).startswith("sha256:")
    authority_claim = expectations["authority_claim"]
    assert str(authority_claim["matrix_digest"]).startswith("sha256:")
    assert authority_claim["target_normative_authority"] == "rust"
    assert authority_claim["implicit_reference_fallback"] is False
    assert expectations["suites"]["compiler"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["compiler"]["implementation_version"] == (
        "0.1.0"
    )
    assert expectations["suites"]["compiler"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["compiler"]["authority_claim"] == (
        authority_claim["suite_claims"]["compiler"]
    )
    assert expectations["suites"]["compiler"]["authority_claim"][
        "comparison"
    ] == "exact-native-reference"
    assert expectations["suites"]["compiler"]["execution_claim"] == {
        "executor_id": "rust-compiler-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["compiler"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["application-events"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["application-events"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["application-events"]["execution_claim"] == {
        "executor_id": "rust-application-events-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["application-events"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["outcome"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["outcome"]["implementation_version"] == (
        "0.1.0"
    )
    assert expectations["suites"]["outcome"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["outcome"]["execution_claim"] == {
        "executor_id": "rust-outcome-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["outcome"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["retry"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["retry"]["implementation_version"] == (
        "0.1.0"
    )
    assert expectations["suites"]["retry"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["retry"]["execution_claim"] == {
        "executor_id": "rust-retry-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["retry"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["sequence"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["sequence"]["implementation_version"] == (
        "0.1.0"
    )
    assert expectations["suites"]["sequence"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["sequence"]["execution_claim"] == {
        "executor_id": "rust-sequence-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["sequence"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["tool-execution"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["tool-execution"][
        "implementation_version"
    ] == "0.1.0"
    assert expectations["suites"]["tool-execution"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["tool-execution"]["execution_claim"] == {
        "executor_id": "rust-tool-execution-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["tool-execution"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["tool-lifecycle"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["tool-lifecycle"][
        "implementation_version"
    ] == "0.1.0"
    assert expectations["suites"]["tool-lifecycle"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["tool-lifecycle"]["execution_claim"] == {
        "executor_id": "rust-tool-lifecycle-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["tool-lifecycle"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["tool-result"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["tool-result"][
        "implementation_version"
    ] == "0.1.0"
    assert expectations["suites"]["tool-result"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["tool-result"]["execution_claim"] == {
        "executor_id": "rust-tool-result-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["tool-result"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["typed-ports"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["typed-ports"][
        "implementation_version"
    ] == "0.1.0"
    assert expectations["suites"]["typed-ports"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["typed-ports"]["execution_claim"] == {
        "executor_id": "rust-typed-ports-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["typed-ports"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert expectations["suites"]["runtime"]["implementation"] == (
        "graphblocks-runtime"
    )
    assert expectations["suites"]["runtime"]["implementation_version"] == (
        "0.1.0"
    )
    assert expectations["suites"]["runtime"][
        "implementation_artifact_distribution"
    ] == "graphblocks-runtime"
    assert expectations["suites"]["runtime"]["execution_claim"] == {
        "executor_id": "rust-runtime-exact-differential",
        "implementation": "graphblocks-runtime",
        "language": "rust",
        "comparison": "exact-native-reference",
        "reference_implementation": "graphblocks-python",
    }
    assert expectations["suites"]["runtime"][
        "reference_implementation_version"
    ] == GRAPHBLOCKS_VERSION
    assert {
        expectation["implementation"]
        for suite, expectation in expectations["suites"].items()
        if suite
        not in {
            "application-events",
            "compiler",
            "outcome",
            "retry",
            "runtime",
            "sequence",
            "tool-execution",
            "tool-lifecycle",
            "tool-result",
            "typed-ports",
        }
    } == {"graphblocks-python"}


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

    native_wheel: Path | None = None

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal native_wheel
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
                wheel = _write_mock_wheel(
                    module,
                    source_root=manifest_root,
                    output_root=output_root,
                )
                if wheel.name.startswith("graphblocks_runtime-"):
                    native_wheel = wheel
        if any(
            "native_extension_status" in part
            or part.endswith("native-authority-probe.py")
            for part in command
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    _native_binding_payload(
                        native_artifact=module.native_runtime_wheel_member_artifact(
                            native_wheel.read_bytes(),
                            distribution_version="0.1.0",
                        )
                        if native_wheel is not None
                        else None
                    )
                ),
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
    root_project = module.tomllib.loads(
        (module.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    root_version = root_project["version"]
    package_boundary = module.yaml.safe_load(
        (module.ROOT / "compatibility" / "python-package-boundaries.yaml").read_text(
            encoding="utf-8"
        )
    )
    stable_surface = module.yaml.safe_load(
        (module.ROOT / package_boundary["rootFacade"]["stableSurface"]).read_text(
            encoding="utf-8"
        )
    )
    expected_root_exports: list[str] = []
    for entry in stable_surface["symbols"]:
        root_export = entry["path"].split(".", 2)[1]
        if root_export not in expected_root_exports:
            expected_root_exports.append(root_export)
    metadata_requirements = list(root_project["dependencies"])
    for extra, requirements in root_project["optional-dependencies"].items():
        metadata_requirements.extend(
            f"{requirement}; extra == '{extra}'" for requirement in requirements
        )
    root_import_budget = package_boundary["coldImportBudgets"]["graphblocks"]
    canonical_import_budget = package_boundary["coldImportBudgets"][
        "graphblocks.canonical"
    ]
    base_probe_payload = {
        "canonicalModules": canonical_import_budget["allowedGraphblocksModules"],
        "distributionVersion": root_version,
        "graphblocksDistributions": ["graphblocks"],
        "requirements": sorted(
            str(module.Requirement(requirement))
            for requirement in metadata_requirements
        ),
        "rootAll": expected_root_exports,
        "rootAttributes": root_import_budget["maxRootAttributes"],
        "rootModules": root_import_budget["allowedGraphblocksModules"],
        "rootPublicNames": sorted(expected_root_exports),
        "stableResolved": expected_root_exports,
    }
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
    native_binding_commands: list[list[str]] = []
    native_wheel: Path | None = None

    class FakeEnvBuilder:
        def __init__(self, *, with_pip: bool) -> None:
            assert with_pip

        def create(self, path: str) -> None:
            (Path(path) / "bin").mkdir(parents=True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal native_wheel
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
                wheel = _write_mock_wheel(
                    module,
                    source_root=manifest_root,
                    output_root=output_root,
                )
                if wheel.name.startswith("graphblocks_runtime-"):
                    native_wheel = wheel
        if "download" in command and "--dest" in command:
            dependency_root = Path(command[command.index("--dest") + 1])
            (dependency_root / "jsonschema-4.25.1-py3-none-any.whl").write_bytes(
                b"dependency"
            )
        if any(
            "native_extension_status" in part
            or part.endswith("native-authority-probe.py")
            for part in command
        ):
            native_binding_commands.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    _native_binding_payload(
                        distribution_version=runtime_version,
                        binding_version=runtime_version,
                        native_artifact=module.native_runtime_wheel_member_artifact(
                            native_wheel.read_bytes(),
                            distribution_version=runtime_version,
                        )
                        if native_wheel is not None
                        else None,
                    )
                ),
            )
        if command[-4:] == ["-m", "graphblocks", "schemas", "manifest"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(expected_schema))
        if command[-2:] == ["-c", module.BASE_GRAPHBLOCKS_INSTALL_PROBE]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(base_probe_payload),
            )
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
    tck_commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_json_command(
        command: list[str],
        *,
        kind: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "TCK":
            tck_commands.append((command, kwargs))
        return _with_content_digest(
            module,
            {"ok": True, "kind": kind},
        )

    monkeypatch.setattr(module, "_run_json_command", fake_json_command)
    artifact_record = module._artifact_record
    runtime_artifact_records: list[dict[str, object]] = []

    def recording_artifact_record(path: Path) -> dict[str, object]:
        record = artifact_record(path)
        if (
            path.name.startswith("graphblocks_runtime-")
            and record["artifactType"] == "wheel"
        ):
            runtime_artifact_records.append(record)
        return record

    monkeypatch.setattr(module, "_artifact_record", recording_artifact_record)

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
    for wheel in wheelhouse.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            assert {member.create_system for member in archive.infolist()} == {3}
    assert {path.name for path in sdist_root.glob("*.tar.gz")} == {
        f"graphblocks-{root_version}.tar.gz",
        f"graphblocks_runtime-{runtime_version}.tar.gz",
        f"graphblocks_testing-{testing_version}.tar.gz",
    }
    assert len(wheel_source_roots) == 3
    deterministic_source_root = wheelhouse.parent / ".graphblocks-sdist-extract"
    assert {path.parents[1] for path in wheel_source_roots} == {
        deterministic_source_root
    }
    assert not deterministic_source_root.exists()
    assert {path.name for path in dependency_wheelhouse.glob("*.whl")} == {
        "jsonschema-4.25.1-py3-none-any.whl"
    }
    assert {path.name for path in evidence.iterdir()} == {
        "acceptance.json",
        "platform.json",
        "sbom.cdx.json",
        "tck.json",
    }
    assert len(tck_commands) == 1
    assert len(native_binding_commands) == 1
    tck_command, tck_arguments = tck_commands[0]
    assert "--native-compiler-wheel" in tck_command
    expected_compiler_artifact = tck_arguments["expected_compiler_artifact"]
    runtime_wheel = next(
        path
        for path in wheelhouse.glob("*.whl")
        if path.name.startswith("graphblocks_runtime-")
    )
    assert expected_compiler_artifact == module._artifact_record(runtime_wheel)
    assert len(runtime_artifact_records) >= 3
    assert all(
        record == runtime_artifact_records[0]
        for record in runtime_artifact_records
    )
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


def test_windows_wheelhouse_build_enforces_reproducible_msvc_linking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_wheelhouse_module()
    root = tmp_path / "repo"
    manifest = root / "pyproject.toml"
    root.mkdir()
    manifest.write_text(
        '[project]\nname = "graphblocks"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.platform_module, "system", lambda: "Windows")
    monkeypatch.setattr(
        module,
        "observe_rustc_identity",
        lambda _rustc: {"version": "rustc 1.94.0", "verbose": "mock"},
    )
    monkeypatch.setattr(module, "_pinned_build_tool_identities", lambda: {})
    monkeypatch.setattr(module, "_resolved_build_environment", lambda **_kwargs: {})
    monkeypatch.setattr(module, "load_package_catalog", lambda: {})
    monkeypatch.setattr(
        module,
        "build_wheel_matrix",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            targets=(SimpleNamespace(manifest="pyproject.toml"),),
            diagnostics=(),
        ),
    )

    observed_environments: list[dict[str, str]] = []

    class ExpectedStop(Exception):
        pass

    def stop_after_first_build(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | None:
        assert "build" in command
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        observed_environments.append(environment)
        raise ExpectedStop

    monkeypatch.setattr(module.subprocess, "run", stop_after_first_build)
    monkeypatch.setenv("RUSTFLAGS", "-C opt-level=1")
    with pytest.raises(RuntimeError, match="inherited Rust compiler flags"):
        module.main(["--wheelhouse", str(tmp_path / "conflicting-wheelhouse")])

    monkeypatch.delenv("RUSTFLAGS")
    monkeypatch.setenv("CARGO_INCREMENTAL", "1")
    with pytest.raises(ExpectedStop):
        module.main(["--wheelhouse", str(tmp_path / "wheelhouse")])

    assert len(observed_environments) == 1
    assert observed_environments[0]["CARGO_INCREMENTAL"] == "0"
    assert observed_environments[0]["CARGO_ENCODED_RUSTFLAGS"] == (
        "-C\x1flink-arg=/Brepro"
    )
