"""The graphblocks-tck command and installed-runtime verification."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import importlib
from importlib import resources
from importlib.metadata import (
    PackageNotFoundError,
    distribution as installed_distribution,
    version as distribution_version,
)
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import BinaryIO
import zipfile

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

from graphblocks.canonical import (
    canonical_dumps_reference as canonical_dumps,
    canonical_hash_reference as canonical_hash,
)
from graphblocks._version import __version__ as GRAPHBLOCKS_VERSION
from graphblocks.conformance import ConformanceAuthorityMatrix
from graphblocks.loader import load_documents
from graphblocks.schema import (
    SchemaManifest,
)

from ._facade import facade_dependency
from .acceptance import AcceptanceGateRunner, AcceptanceManifest
from .fixture_loading import (
    _tck_fixture_digest,
    _tck_registry,
    bundled_tck_root,
    load_bundled_tck_cases_for_suite,
    load_bundled_tck_suite_manifests,
    load_tck_cases_for_suite,
    load_tck_suite_manifests,
)
from .models import _BUNDLED_TCK_SUITES, _STABLE_RELEASE_PROFILES
from .profiles import ConformanceProfileSet, check_tck_suite_coverage
from .reports import TckReport
from .runners import (
    TckRunner,
    _ApplicationEventStreamDifferentialTckRunner,
    _NormativeCompilerTckRunner,
    _NormativeRuntimeTckRunner,
    _RetryDifferentialTckRunner,
)


def _native_compiler_version() -> str:
    version_lookup = facade_dependency(
        "distribution_version",
        distribution_version,
    )
    try:
        version = version_lookup("graphblocks-runtime")
    except PackageNotFoundError:
        try:
            import graphblocks_runtime
        except ImportError:
            return "unavailable"
        version = getattr(graphblocks_runtime, "__version__", None)
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("native compiler implementation version is unavailable")
    return version


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def _verified_regular_file(
    path: Path,
    *,
    label: str,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} must be a readable regular file") from error
    if not stat.S_ISREG(path_status.st_mode):
        raise ValueError(f"{label} must be a regular file")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} must be a readable regular file") from error
    try:
        file = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise
    with file:
        opened_status = os.fstat(file.fileno())
        try:
            current_status = path.lstat()
        except OSError as error:
            raise ValueError(f"{label} changed while it was opened") from error
        # Windows 3.12 path stat preserves creation time in st_ctime_ns while
        # descriptor stat exposes metadata change time.  Bind the two views by
        # file identity and content metadata, and compare each API's full stat
        # result separately below.
        opened_cross_api_identity = (
            opened_status.st_dev,
            opened_status.st_ino,
            stat.S_IFMT(opened_status.st_mode),
            opened_status.st_size,
            opened_status.st_mtime_ns,
        )
        current_cross_api_identity = (
            current_status.st_dev,
            current_status.st_ino,
            stat.S_IFMT(current_status.st_mode),
            current_status.st_size,
            current_status.st_mtime_ns,
        )
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or _stat_identity(current_status) != _stat_identity(path_status)
            or current_cross_api_identity != opened_cross_api_identity
        ):
            raise ValueError(f"{label} changed while it was opened")
        yield file, opened_status
        final_status = os.fstat(file.fileno())
        try:
            current_status = path.lstat()
        except OSError as error:
            raise ValueError(f"{label} changed while it was read") from error
        final_cross_api_identity = (
            final_status.st_dev,
            final_status.st_ino,
            stat.S_IFMT(final_status.st_mode),
            final_status.st_size,
            final_status.st_mtime_ns,
        )
        current_cross_api_identity = (
            current_status.st_dev,
            current_status.st_ino,
            stat.S_IFMT(current_status.st_mode),
            current_status.st_size,
            current_status.st_mtime_ns,
        )
        if (
            _stat_identity(final_status) != _stat_identity(opened_status)
            or _stat_identity(current_status) != _stat_identity(path_status)
            or final_cross_api_identity != current_cross_api_identity
        ):
            raise ValueError(f"{label} changed while it was read")


def _installed_runtime_from_wheel(
    archive: zipfile.ZipFile,
) -> object:
    archive_members = archive.infolist()
    archive_names = [member.filename for member in archive_members]
    if len(archive_names) != len(set(archive_names)):
        raise ValueError("native compiler wheel contains duplicate members")
    runtime_members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    native_members: list[PurePosixPath] = []
    for member in archive_members:
        archive_path = PurePosixPath(member.filename)
        if (
            archive_path.is_absolute()
            or ".." in archive_path.parts
            or not archive_path.parts
        ):
            raise ValueError("native compiler wheel contains an unsafe member")
        if (
            not member.is_dir()
            and archive_path.parts[0] != "graphblocks_runtime"
            and not archive_path.parts[0].endswith(".dist-info")
        ):
            raise ValueError(
                "native compiler wheel contains an unexpected install payload"
            )
        if member.is_dir() or archive_path.parts[0] != "graphblocks_runtime":
            continue
        relative_path = PurePosixPath(*archive_path.parts[1:])
        if (
            not relative_path.parts
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise ValueError("native compiler wheel contains an unsafe runtime member")
        if "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
            raise ValueError(
                "native compiler wheel must not contain runtime bytecode caches"
            )
        runtime_members.append((member, relative_path))
        if (
            len(relative_path.parts) == 1
            and relative_path.name.startswith("_native.")
            and any(
                relative_path.name.endswith(suffix)
                for suffix in importlib.machinery.EXTENSION_SUFFIXES
            )
        ):
            native_members.append(relative_path)
    runtime_paths = {relative_path for _member, relative_path in runtime_members}
    if (
        not {PurePosixPath("__init__.py"), PurePosixPath("py.typed")} <= runtime_paths
        or len(native_members) != 1
    ):
        raise ValueError(
            "native compiler wheel must contain the typed runtime package and "
            "exactly one native extension"
        )

    try:
        distribution_lookup = facade_dependency(
            "installed_distribution",
            installed_distribution,
        )
        runtime_distribution = distribution_lookup("graphblocks-runtime")
    except PackageNotFoundError as error:
        raise RuntimeError("graphblocks-runtime is not installed") from error
    runtime_root = Path(runtime_distribution.locate_file("graphblocks_runtime"))
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RuntimeError(
            "installed graphblocks-runtime package must be a regular directory"
        )
    installed_runtime_paths: set[PurePosixPath] = set()
    for installed_path in runtime_root.rglob("*"):
        relative = installed_path.relative_to(runtime_root)
        relative_path = PurePosixPath(*relative.parts)
        if "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
            continue
        if installed_path.is_symlink():
            raise RuntimeError(
                "installed graphblocks-runtime package contains a symlink"
            )
        if installed_path.is_file():
            installed_runtime_paths.add(relative_path)
    unexpected_installed_paths = sorted(installed_runtime_paths - runtime_paths)
    if unexpected_installed_paths:
        raise RuntimeError(
            "installed graphblocks-runtime package contains files outside the "
            "selected wheel: "
            + ", ".join(path.as_posix() for path in unexpected_installed_paths)
        )
    graphblocks_runtime: object | None = None
    for verification_phase in range(2):
        for member, relative_path in runtime_members:
            installed_path = runtime_root.joinpath(*relative_path.parts)
            with (
                archive.open(member, "r") as wheel_member,
                _verified_regular_file(
                    installed_path,
                    label=f"installed runtime member {relative_path.as_posix()}",
                ) as (installed_member, installed_status),
            ):
                if member.file_size != installed_status.st_size:
                    raise RuntimeError(
                        "installed graphblocks-runtime bytes do not match the selected wheel"
                    )
                while True:
                    expected_chunk = wheel_member.read(1024 * 1024)
                    observed_chunk = installed_member.read(
                        len(expected_chunk) if expected_chunk else 1
                    )
                    if expected_chunk != observed_chunk:
                        raise RuntimeError(
                            "installed graphblocks-runtime bytes do not match "
                            "the selected wheel"
                        )
                    if not expected_chunk:
                        break
        if verification_phase != 0:
            continue
        graphblocks_runtime = importlib.import_module("graphblocks_runtime")
        package_file = getattr(graphblocks_runtime, "__file__", None)
        if not isinstance(package_file, str):
            raise RuntimeError(
                "loaded graphblocks-runtime package has no file identity"
            )
        try:
            if not os.path.samefile(package_file, runtime_root / "__init__.py"):
                raise RuntimeError(
                    "loaded graphblocks-runtime package is not the installed distribution"
                )
        except OSError as error:
            raise RuntimeError(
                "loaded graphblocks-runtime package identity is unavailable"
            ) from error
        native_module = importlib.import_module("graphblocks_runtime._native")
        native_file = getattr(native_module, "__file__", None)
        if not isinstance(native_file, str):
            raise RuntimeError(
                "loaded graphblocks-runtime native module has no file identity"
            )
        try:
            if not os.path.samefile(
                native_file,
                runtime_root.joinpath(*native_members[0].parts),
            ):
                raise RuntimeError(
                    "loaded graphblocks-runtime native module is not from "
                    "the selected package"
                )
        except OSError as error:
            raise RuntimeError(
                "loaded graphblocks-runtime native module identity is unavailable"
            ) from error
    if graphblocks_runtime is None:
        raise RuntimeError("graphblocks-runtime could not be loaded")
    return graphblocks_runtime


def _native_compiler_wheel_artifact(path: Path) -> dict[str, object]:
    try:
        distribution, version, _build, wheel_tags = parse_wheel_filename(path.name)
    except ValueError as error:
        raise ValueError("native compiler artifact must be a valid wheel") from error
    if canonicalize_name(str(distribution)) != "graphblocks-runtime":
        raise ValueError("native compiler artifact must be a graphblocks-runtime wheel")
    compatible_tags = facade_dependency("sys_tags", sys_tags)
    if wheel_tags.isdisjoint(compatible_tags()):
        raise ValueError(
            "native compiler wheel is not compatible with the running interpreter"
        )
    version_lookup = facade_dependency(
        "_native_compiler_version",
        _native_compiler_version,
    )
    installed_version = version_lookup()
    if str(version) != installed_version:
        raise ValueError(
            "native compiler wheel version does not match the installed distribution"
        )
    with _verified_regular_file(
        path,
        label="native compiler wheel",
    ) as (wheel, wheel_status):
        digest = hashlib.sha256()
        while chunk := wheel.read(1024 * 1024):
            digest.update(chunk)
        wheel.seek(0)
        try:
            with zipfile.ZipFile(wheel) as archive:
                graphblocks_runtime = _installed_runtime_from_wheel(archive)
        except zipfile.BadZipFile as error:
            raise ValueError(
                "native compiler artifact must be a valid wheel"
            ) from error
        native_extension_available = getattr(
            graphblocks_runtime,
            "native_extension_available",
            None,
        )
        if (
            not callable(native_extension_available)
            or native_extension_available() is not True
        ):
            raise RuntimeError("graphblocks-runtime native extension is unavailable")
        binding_version = getattr(graphblocks_runtime, "binding_version", None)
        if not callable(binding_version) or binding_version() != installed_version:
            raise RuntimeError(
                "native compiler binding version does not match the installed distribution"
            )
    return {
        "filename": path.name,
        "sha256": digest.hexdigest(),
        "size": wheel_status.st_size,
        "distribution": "graphblocks-runtime",
        "version": installed_version,
        "artifactType": "wheel",
    }


def run_bundled_tck_suite(
    suite: str,
    *,
    profile: str = "local",
    evidence_dir: str | Path | None = None,
) -> TckReport:
    """Run one bundled C0/C1 suite and return its deterministic report."""

    manifest_by_suite = {
        manifest.suite_id: manifest for manifest in load_bundled_tck_suite_manifests()
    }
    manifest = manifest_by_suite.get(suite)
    if manifest is None:
        raise ValueError(f"TCK suite {suite!r} is not bundled with graphblocks-testing")
    if profile == "native" and suite != "runtime":
        raise ValueError("native TCK profile is supported only for the runtime suite")
    return TckRunner(
        _tck_registry(suite),
        profile=profile,
        evidence_dir=Path(evidence_dir) if evidence_dir is not None else None,
        suite=suite,
        fixture_digest=manifest.fixture_digest,
    ).run_cases(load_bundled_tck_cases_for_suite(suite))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphblocks-tck")
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser("list", help="list shared TCK suite manifests")
    list_parser.add_argument("root", nargs="?", type=Path)
    list_parser.add_argument("--json", action="store_true", help="emit JSON")
    check_parser = subparsers.add_parser(
        "check", help="check TCK fixture coverage for conformance profiles"
    )
    check_parser.add_argument("root", nargs="?", type=Path)
    check_parser.add_argument(
        "--profiles", required=True, type=Path, help="conformance profile YAML document"
    )
    check_parser.add_argument(
        "--profile",
        dest="profile_ids",
        action="append",
        required=True,
        help="claimed profile id",
    )
    check_parser.add_argument("--json", action="store_true", help="emit JSON")
    run_parser = subparsers.add_parser("run", help="run a shared TCK fixture")
    run_parser.add_argument(
        "suite",
        choices=(
            "application-events",
            "application-protocol",
            "approval-review",
            "compiler",
            "conversation",
            "deployment",
            "durable",
            "documents",
            "migration",
            "runtime",
            "orchestration",
            "schema",
            "policy",
            "rag",
            "retry",
            "sequence",
            "exhaustion",
            "budget-race",
            "tool-lifecycle",
            "tool-execution",
            "tool-result",
            "usage",
            "voice",
        ),
        help="TCK suite kind",
    )
    run_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="cases.json fixture path; defaults to the bundled C0/C1 fixture",
    )
    run_parser.add_argument(
        "--profile", default="local", help="profile label for the generated report"
    )
    run_parser.add_argument(
        "--evidence-dir", type=Path, help="directory for native runtime SQLite evidence"
    )
    run_parser.add_argument("--json", action="store_true", help="emit JSON")
    run_all_parser = subparsers.add_parser(
        "run-all", help="run every supported shared TCK fixture under a root"
    )
    run_all_parser.add_argument("root", nargs="?", type=Path)
    run_all_parser.add_argument(
        "--profile", default="local", help="profile label for the generated reports"
    )
    run_all_parser.add_argument(
        "--evidence-dir", type=Path, help="directory for native runtime SQLite evidence"
    )
    run_all_parser.add_argument(
        "--native-compiler-wheel",
        type=Path,
        help="bind compiler and local-runtime TCK evidence to an exact installed runtime wheel",
    )
    run_all_parser.add_argument("--json", action="store_true", help="emit JSON")
    acceptance_parser = subparsers.add_parser(
        "run-acceptance",
        help="run exact registered gates from an acceptance application manifest",
    )
    acceptance_parser.add_argument(
        "manifest", type=Path, help="acceptance application manifest"
    )
    acceptance_parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="root used to resolve scenario paths",
    )
    acceptance_parser.add_argument("--json", action="store_true", help="emit JSON")

    args = parser.parse_args(argv)
    if args.command == "run-acceptance":
        documents = load_documents(args.manifest)
        if not documents:
            raise ValueError("acceptance application manifest must not be empty")
        report = AcceptanceGateRunner().run_manifest(
            AcceptanceManifest.from_document(documents[0]),
            root=args.root,
        )
        payload = report.report_contract()
        payload["contentDigest"] = report.content_digest()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"{'OK' if report.ok else 'FAILED'} "
                f"{len(report.applications)} acceptance applications"
            )
            for application in report.applications:
                if not application.ok:
                    print(f"{application.application_id} failed")
        return 0 if report.ok else 1
    if args.command == "list":
        manifests = (
            load_bundled_tck_suite_manifests()
            if args.root is None
            else load_tck_suite_manifests(args.root)
        )
        payload = {
            "suiteCount": len(manifests),
            "suites": [manifest.manifest_contract() for manifest in manifests],
        }
        payload["contentDigest"] = canonical_hash(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for manifest in manifests:
                print(
                    f"{manifest.suite_id} cases={manifest.case_count} path={manifest.path}"
                )
        return 0
    if args.command == "check":
        documents = load_documents(args.profiles)
        if not documents:
            raise ValueError("conformance profile document must not be empty")
        coverage = check_tck_suite_coverage(
            ConformanceProfileSet.from_document(documents[0]),
            tuple(args.profile_ids),
            (
                load_bundled_tck_suite_manifests()
                if args.root is None
                else load_tck_suite_manifests(args.root)
            ),
        )
        payload = coverage.coverage_contract()
        payload["contentDigest"] = coverage.content_digest()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif coverage.ok:
            print(f"OK {len(coverage.claim.tck_suites)} TCK suites covered")
        else:
            for issue in coverage.issues:
                print(f"{issue.code} {issue.suite}: {issue.message}")
        return 0 if coverage.ok else 1
    if args.command == "run":
        if args.profile == "native" and args.suite != "runtime":
            payload = {
                "ok": False,
                "error": "native TCK profile is supported only for the runtime suite",
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(payload["error"])
            return 1
        if args.path is None and args.suite not in _BUNDLED_TCK_SUITES:
            payload = {
                "ok": False,
                "error": (
                    f"TCK suite {args.suite!r} is not bundled with graphblocks-testing; "
                    "provide an explicit cases.json path"
                ),
            }
            if args.json:
                print(canonical_dumps(payload))
            else:
                print(payload["error"])
            return 1
        if args.path is None:
            report = run_bundled_tck_suite(
                args.suite,
                profile=args.profile,
                evidence_dir=args.evidence_dir,
            )
        else:
            cases = load_tck_cases_for_suite(args.suite, args.path)
            report = TckRunner(
                _tck_registry(args.suite),
                profile=args.profile,
                evidence_dir=args.evidence_dir,
                suite=args.suite,
                fixture_digest=_tck_fixture_digest(args.suite, args.path),
            ).run_cases(cases)
        payload = report.report_contract()
        payload["contentDigest"] = report.content_digest()
        if args.json:
            print(canonical_dumps(payload))
        else:
            print(
                f"{'OK' if report.ok else 'FAILED'} {len(report.results)} {args.suite} TCK cases"
            )
            for result in report.results:
                if result.status != "passed":
                    print(f"{result.case_id} {result.status}")
        return 0 if report.ok else 1
    if args.command == "run-all":
        tck_root = bundled_tck_root() if args.root is None else args.root
        manifests = (
            load_bundled_tck_suite_manifests()
            if args.root is None
            else load_tck_suite_manifests(tck_root)
        )
        compiler_artifact = None
        if args.native_compiler_wheel is not None:
            if args.root is not None or args.profile != "local":
                raise ValueError(
                    "--native-compiler-wheel requires bundled local TCK execution"
                )
            if not any(manifest.suite_id == "compiler" for manifest in manifests):
                raise ValueError(
                    "--native-compiler-wheel requires the compiler TCK suite"
                )
            if not any(manifest.suite_id == "runtime" for manifest in manifests):
                raise ValueError(
                    "--native-compiler-wheel requires the runtime TCK suite"
                )
            artifact_loader = facade_dependency(
                "_native_compiler_wheel_artifact",
                _native_compiler_wheel_artifact,
            )
            compiler_artifact = artifact_loader(args.native_compiler_wheel)
        if args.profile == "native" and any(
            manifest.suite_id != "runtime" for manifest in manifests
        ):
            payload = {
                "ok": False,
                "error": "native TCK profile is supported only for the runtime suite",
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(payload["error"])
            return 1
        reports: dict[str, dict[str, object]] = {}
        observed_execution_claims: dict[str, dict[str, str]] = {}
        ok = True
        for manifest in manifests:
            evidence_dir = (
                args.evidence_dir / manifest.suite_id
                if args.evidence_dir is not None
                else None
            )
            fixture_path = tck_root / manifest.path
            if (
                manifest.suite_id == "application-events"
                and compiler_artifact is not None
            ):
                runner = _ApplicationEventStreamDifferentialTckRunner(
                    _tck_registry(manifest.suite_id),
                    profile=args.profile,
                    evidence_dir=evidence_dir,
                    suite=manifest.suite_id,
                    fixture_digest=manifest.fixture_digest,
                    implementation="graphblocks-runtime",
                    implementation_version=facade_dependency(
                        "_native_compiler_version",
                        _native_compiler_version,
                    )(),
                )
            elif manifest.suite_id == "compiler" and compiler_artifact is not None:
                runner = _NormativeCompilerTckRunner(
                    _tck_registry(manifest.suite_id),
                    profile=args.profile,
                    evidence_dir=evidence_dir,
                    suite=manifest.suite_id,
                    fixture_digest=manifest.fixture_digest,
                    implementation="graphblocks-runtime",
                    implementation_version=facade_dependency(
                        "_native_compiler_version",
                        _native_compiler_version,
                    )(),
                )
            elif manifest.suite_id == "runtime" and compiler_artifact is not None:
                runner = _NormativeRuntimeTckRunner(
                    _tck_registry(manifest.suite_id),
                    profile=args.profile,
                    evidence_dir=evidence_dir,
                    suite=manifest.suite_id,
                    fixture_digest=manifest.fixture_digest,
                    implementation="graphblocks-runtime",
                    implementation_version=facade_dependency(
                        "_native_compiler_version",
                        _native_compiler_version,
                    )(),
                )
            elif manifest.suite_id == "retry" and compiler_artifact is not None:
                runner = _RetryDifferentialTckRunner(
                    _tck_registry(manifest.suite_id),
                    profile=args.profile,
                    evidence_dir=evidence_dir,
                    suite=manifest.suite_id,
                    fixture_digest=manifest.fixture_digest,
                    implementation="graphblocks-runtime",
                    implementation_version=facade_dependency(
                        "_native_compiler_version",
                        _native_compiler_version,
                    )(),
                )
            else:
                runner = TckRunner(
                    _tck_registry(manifest.suite_id),
                    profile=args.profile,
                    evidence_dir=evidence_dir,
                    suite=manifest.suite_id,
                    fixture_digest=manifest.fixture_digest,
                )
            report = runner.run_cases(
                load_tck_cases_for_suite(manifest.suite_id, fixture_path)
            )
            report_contract = report.report_contract()
            evidence = dict(report_contract["evidence"])
            evidence["case_ids_digest"] = canonical_hash(
                {"case_ids": list(manifest.case_ids)}
            )
            evidence["suite_manifest_digest"] = manifest.content_digest()
            if (
                manifest.suite_id
                in {"application-events", "compiler", "retry", "runtime"}
                and compiler_artifact is not None
            ):
                evidence["implementation_artifact"] = dict(compiler_artifact)
            observed_execution_claims[manifest.suite_id] = {
                "executor_id": runner.authority_executor_id,
                "implementation": report.implementation,
                "language": runner.authority_language,
                "comparison": runner.authority_comparison,
                "reference_implementation": (
                    runner.authority_reference_implementation
                ),
            }
            report_contract["evidence"] = evidence
            reports[manifest.suite_id] = report_contract
            ok = ok and report.ok
        payload = {
            "profile": args.profile,
            "ok": ok,
            "reports": reports,
        }
        if args.root is None and args.profile == "local":
            profile_resource = resources.files("graphblocks").joinpath(
                "data", "conformance-profiles.yaml"
            )
            with resources.as_file(profile_resource) as profile_path:
                profile_documents = load_documents(profile_path)
            if len(profile_documents) != 1:
                raise ValueError(
                    "installed conformance profile catalog must contain one document"
                )
            profile_set = ConformanceProfileSet.from_document(profile_documents[0])
            authority_resource = resources.files("graphblocks").joinpath(
                "data", "stable-release-matrix.yaml"
            )
            if authority_resource.is_file():
                with resources.as_file(authority_resource) as authority_path:
                    authority_documents = load_documents(authority_path)
            else:
                checkout_authority_path = (
                    Path(__file__).resolve().parents[4]
                    / "docs"
                    / "project"
                    / "stable-release-matrix.yaml"
                )
                authority_documents = load_documents(checkout_authority_path)
            if len(authority_documents) != 1 or not isinstance(
                authority_documents[0], Mapping
            ):
                raise ValueError(
                    "installed conformance authority matrix must contain one document"
                )
            authority_matrix = ConformanceAuthorityMatrix.from_document(
                authority_documents[0]
            )
            authority_claim = authority_matrix.validate_tck_claims(
                claimed_profiles=_STABLE_RELEASE_PROFILES,
                declared_suites_by_profile={
                    profile_id: profile_set.by_id(profile_id).tck_suites
                    for profile_id in _STABLE_RELEASE_PROFILES
                },
                observed_execution_claims=observed_execution_claims,
            )
            suite_authority_claims = authority_claim.get("suite_claims")
            if not isinstance(suite_authority_claims, Mapping) or set(
                suite_authority_claims
            ) != set(reports):
                raise ValueError(
                    "installed conformance authority claims do not cover TCK reports"
                )
            reference_implementation_version = GRAPHBLOCKS_VERSION
            for suite_id, report in reports.items():
                evidence = report.get("evidence")
                suite_authority_claim = suite_authority_claims.get(suite_id)
                if not isinstance(evidence, dict) or not isinstance(
                    suite_authority_claim, Mapping
                ):
                    raise ValueError(
                        "installed TCK report cannot bind its authority claim"
                    )
                evidence["authority_claim"] = dict(suite_authority_claim)
                execution_claim = observed_execution_claims[suite_id]
                evidence["execution_claim"] = dict(execution_claim)
                if execution_claim["comparison"] == "exact-native-reference":
                    evidence["reference_implementation_version"] = (
                        reference_implementation_version
                    )
            schema_resource = resources.files("graphblocks").joinpath("schemas")
            if schema_resource.is_dir():
                with resources.as_file(schema_resource) as schema_root:
                    schema_manifest_digest = SchemaManifest.from_directory(
                        schema_root
                    ).content_digest()
            else:
                checkout_schema_root = Path(__file__).resolve().parents[4] / "schemas"
                schema_manifest_digest = SchemaManifest.from_directory(
                    checkout_schema_root
                ).content_digest()
            payload.update(
                {
                    "suite_manifest_digest": canonical_hash(
                        {
                            "suites": [
                                manifest.manifest_contract() for manifest in manifests
                            ]
                        }
                    ),
                    "claimed_profiles": list(_STABLE_RELEASE_PROFILES),
                    "authority_claim": authority_claim,
                    "profile_catalog_digest": canonical_hash(profile_documents[0]),
                    "schema_manifest_digest": schema_manifest_digest,
                }
            )
        payload["contentDigest"] = canonical_hash(payload)
        if args.json:
            print(canonical_dumps(payload))
        else:
            print(f"{'OK' if ok else 'FAILED'} {len(reports)} TCK suites")
            for suite_id, report in reports.items():
                if not report["ok"]:
                    print(f"{suite_id} failed")
        return 0 if ok else 1
    parser.print_help()
    return 0
