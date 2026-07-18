from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
import tomllib
from typing import NamedTuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

from graphblocks.canonical import canonical_dumps, canonical_hash

try:
    from tools.verify_wheelhouse import (
        CYCLONEDX_BOM_VERSION,
        PINNED_BUILD_TOOLS,
        PINNED_RUSTC_VERSION,
        parse_rustc_identity,
        release_evidence_expectations,
        validate_release_evidence_payloads,
    )
except ModuleNotFoundError:  # Direct `python tools/release_supply_chain.py` execution.
    from verify_wheelhouse import (  # type: ignore[no-redef]
        CYCLONEDX_BOM_VERSION,
        PINNED_BUILD_TOOLS,
        PINNED_RUSTC_VERSION,
        parse_rustc_identity,
        release_evidence_expectations,
        validate_release_evidence_payloads,
    )


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
EXPECTED_EVIDENCE = ("acceptance.json", "platform.json", "tck.json")
EXPECTATIONS_NAME = "release-expectations.json"
SIGSTORE_ISSUER = "https://token.actions.githubusercontent.com"
SIGSTORE_REPOSITORY = "graphblocks/graphblocks"
SIGSTORE_WORKFLOW = ".github/workflows/ci.yml"
PROMOTION_SIGSTORE_WORKFLOW = ".github/workflows/promotion-reports.yml"
SIGNATURE_BUNDLE_NAME = "release-manifest.sigstore.json"
PROMOTION_EVIDENCE_NAME = "promotion-evidence.json"
PROMOTION_REPORT_TYPES = (
    "candidate-manifest",
    "soak-application",
    "api-review",
    "security-review",
    "stable-scope",
    "protected-final-ref",
    "staged-rehearsal",
)
PROMOTION_DOCUMENTATION_PATHS = {
    "CHANGELOG.md",
    "README.md",
    "README.ko.md",
    "README.zh-CN.md",
}
PROMOTION_DOCUMENTATION_PREFIX = "docs/project/"
PROMOTION_PYPROJECT_PATHS = {
    "pyproject.toml",
    "packages/graphblocks-testing/pyproject.toml",
}
PROMOTION_VERSION_ONLY_PATHS = {
    "src/graphblocks/__init__.py",
    "compatibility/stable-testing-api.json",
}
PROMOTION_TESTING_CLI_SNAPSHOT_PATH = "compatibility/stable-testing-cli-contracts.json"
RELEASE_REF_PATTERN = r"refs/tags/v1\.0\.0(?:-rc\.[1-9][0-9]*)?"
RELEASE_REF = re.compile(RELEASE_REF_PATTERN)
RELEASE_CANDIDATE_REF = re.compile(r"refs/tags/v1\.0\.0-rc\.[1-9][0-9]*")
SUPPORTED_PLATFORM_MATRIX = (
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.12"),
    ("windows-latest", "3.11"),
    ("windows-latest", "3.12"),
)
MATRIX_PROMOTION_REPORT_KEYS = {
    "runId",
    "status",
    "complete",
    "candidateRef",
    "candidateCommit",
    "candidateManifestDigest",
    "supportedMatrix",
}
CI_RUN_ATTEMPT_ID = re.compile(
    rf"https://github\.com/{re.escape(SIGSTORE_REPOSITORY)}/actions/runs/"
    r"[1-9][0-9]*/attempts/[1-9][0-9]*"
)
RUNTIME_WHEEL_INTERPRETER = "cp311"
SUPPORTED_NATIVE_WHEEL_COUNT = len(
    {os_name for os_name, _python_version in SUPPORTED_PLATFORM_MATRIX}
)
PINNED_COSIGN_VERSION = "3.0.6"
PINNED_RELEASE_TOOLS = {
    **PINNED_BUILD_TOOLS,
    "cyclonedx-bom": CYCLONEDX_BOM_VERSION,
    "cosign": PINNED_COSIGN_VERSION,
    "rustc": PINNED_RUSTC_VERSION,
}
FIRST_PARTY_RUNTIME_DEPENDENCIES = {
    "graphblocks": frozenset({"jsonschema", "packaging", "pyyaml"}),
    "graphblocks-runtime": frozenset(),
    "graphblocks-testing": frozenset({"graphblocks"}),
}


class ReleaseBundleError(RuntimeError):
    """The release bundle is incomplete, inconsistent, or untrusted."""


class FileSnapshot(NamedTuple):
    path: Path
    data: bytes
    size: int
    sha256: str


class PromotionReportArtifact(NamedTuple):
    payload: dict[str, object]
    report_snapshot: FileSnapshot
    signature_snapshot: FileSnapshot
    signature_integrated_at: datetime


def _release_version_from_ref(release_ref: str) -> str:
    if RELEASE_REF.fullmatch(release_ref) is None:
        raise ReleaseBundleError("release ref is not an allowed stable release ref")
    if "-rc." not in release_ref:
        return "1.0.0"
    return "1.0.0rc" + release_ref.rsplit(".", 1)[1]


def _tool_command(executable: str | Sequence[str]) -> list[str]:
    command = [executable] if isinstance(executable, str) else list(executable)
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ReleaseBundleError("tool executable command must not be empty")
    return command


def _parse_cosign_identity(output: str) -> dict[str, str]:
    normalized = output.strip()
    match = re.search(
        r"(?m)^\s*GitVersion:\s*v?([0-9]+\.[0-9]+\.[0-9]+)\s*$",
        normalized,
    )
    if match is None:
        raise ReleaseBundleError("Cosign version returned an unrecognized identity")
    version = match.group(1)
    if version != PINNED_COSIGN_VERSION:
        raise ReleaseBundleError(
            f"release signing requires Cosign {PINNED_COSIGN_VERSION}, found {version!r}"
        )
    return {"version": version, "output": normalized}


def _observe_cosign_identity(
    executable: str | Sequence[str] = "cosign",
) -> dict[str, str]:
    command = [*_tool_command(executable), "version"]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseBundleError(
            f"release signing requires Cosign {PINNED_COSIGN_VERSION}"
        ) from error
    return _parse_cosign_identity(completed.stdout)


def _canonical_json_bytes(value: object) -> bytes:
    return (canonical_dumps(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot_regular_file(path: Path, *, owner: str) -> FileSnapshot:
    try:
        before = path.lstat()
    except OSError as error:
        raise ReleaseBundleError(f"{owner} is missing or unreadable: {path}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseBundleError(f"{owner} must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseBundleError(f"{owner} could not be opened safely: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ReleaseBundleError(f"{owner} changed while it was opened: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ReleaseBundleError(f"{owner} changed while it was read: {path}")
    data = b"".join(chunks)
    if len(data) != after.st_size:
        raise ReleaseBundleError(f"{owner} changed while it was read: {path}")
    return FileSnapshot(path, data, len(data), _sha256_bytes(data))


def _file_record(snapshot: FileSnapshot, *, relative_to: Path) -> dict[str, object]:
    return {
        "path": snapshot.path.relative_to(relative_to).as_posix(),
        "sha256": snapshot.sha256,
        "size": snapshot.size,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json_bytes(value))


def _json_from_snapshot(snapshot: FileSnapshot, *, owner: str) -> dict[str, object]:
    try:
        value = json.loads(snapshot.data.decode("utf-8"), parse_float=Decimal)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseBundleError(f"{owner} is not valid JSON: {snapshot.path}") from error
    if not isinstance(value, dict):
        raise ReleaseBundleError(f"{owner} must be a JSON object: {snapshot.path}")
    return value


def _read_json(path: Path, *, owner: str) -> dict[str, object]:
    return _json_from_snapshot(
        _snapshot_regular_file(path, owner=owner),
        owner=owner,
    )


def _require_sha256(value: object, *, owner: str) -> str:
    if not isinstance(value, str) or CANONICAL_SHA256.fullmatch(value) is None:
        raise ReleaseBundleError(f"{owner} must be a lowercase SHA-256 digest")
    return value


def _require_prefixed_sha256(value: object, *, owner: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ReleaseBundleError(f"{owner} must be a canonical SHA-256 digest")
    _require_sha256(value.removeprefix("sha256:"), owner=owner)
    return value


def _require_exact_keys(
    value: object,
    expected: set[str],
    *,
    owner: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ReleaseBundleError(f"{owner} has an invalid or incomplete shape")
    return value


def _require_nonempty_string(value: object, *, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseBundleError(f"{owner} must be a nonempty string")
    return value


def _require_ci_run_attempt_id(value: object, *, owner: str) -> str:
    if not isinstance(value, str) or CI_RUN_ATTEMPT_ID.fullmatch(value) is None:
        raise ReleaseBundleError(
            f"{owner} must be a canonical graphblocks/graphblocks CI run-attempt identity"
        )
    return value


def _parse_utc_timestamp(value: object, *, owner: str) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ) is None:
        raise ReleaseBundleError(f"{owner} must be a canonical UTC timestamp")
    try:
        observed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ReleaseBundleError(f"{owner} must be a valid UTC timestamp") from error
    if observed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ReleaseBundleError(f"{owner} must be a canonical UTC timestamp")
    return observed


def _require_content_digest(payload: Mapping[str, object], *, owner: str) -> str:
    observed = payload.get("contentDigest")
    if not isinstance(observed, str) or not observed.startswith("sha256:"):
        raise ReleaseBundleError(f"{owner} contentDigest must be a canonical SHA-256 digest")
    _require_sha256(observed.removeprefix("sha256:"), owner=f"{owner} contentDigest")
    unsigned_payload = dict(payload)
    unsigned_payload.pop("contentDigest")
    expected = canonical_hash(unsigned_payload)
    if observed != expected:
        raise ReleaseBundleError(f"{owner} contentDigest does not match its content")
    return observed


def _promotion_path_kind(path: str) -> str | None:
    if path in PROMOTION_DOCUMENTATION_PATHS or path.startswith(
        PROMOTION_DOCUMENTATION_PREFIX
    ):
        return "documentation"
    if path in PROMOTION_PYPROJECT_PATHS:
        return "pyproject"
    if path in PROMOTION_VERSION_ONLY_PATHS:
        return "version-only"
    if path == PROMOTION_TESTING_CLI_SNAPSHOT_PATH:
        return "testing-cli-snapshot"
    return None


def _promote_self_digesting_json(
    value: object,
    *,
    candidate_version: str,
    final_version: str,
) -> object:
    if isinstance(value, str):
        return value.replace(candidate_version, final_version)
    if isinstance(value, list):
        return [
            _promote_self_digesting_json(
                item,
                candidate_version=candidate_version,
                final_version=final_version,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    promoted = {
        key: _promote_self_digesting_json(
            item,
            candidate_version=candidate_version,
            final_version=final_version,
        )
        for key, item in value.items()
    }
    observed_digest = value.get("contentDigest")
    unsigned_value = dict(value)
    unsigned_value.pop("contentDigest", None)
    if isinstance(observed_digest, str) and observed_digest == canonical_hash(unsigned_value):
        unsigned_promoted = dict(promoted)
        unsigned_promoted.pop("contentDigest", None)
        promoted["contentDigest"] = canonical_hash(unsigned_promoted)
    return promoted


def _promoted_testing_cli_snapshot(
    candidate_data: bytes,
    *,
    candidate_version: str,
    final_version: str,
) -> bytes:
    try:
        candidate_payload = json.loads(candidate_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBundleError(
            "stable testing CLI compatibility snapshot is not valid JSON"
        ) from error
    if not isinstance(candidate_payload, dict):
        raise ReleaseBundleError(
            "stable testing CLI compatibility snapshot must contain an object"
        )
    promoted = _promote_self_digesting_json(
        candidate_payload,
        candidate_version=candidate_version,
        final_version=final_version,
    )
    return (
        json.dumps(promoted, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _validate_promotion_source_diff_shape(value: object) -> dict[str, object]:
    source_diff = _require_exact_keys(
        value,
        {"digest", "changes"},
        owner="stable promotion source diff",
    )
    _require_prefixed_sha256(
        source_diff.get("digest"), owner="stable promotion source diff digest"
    )
    raw_changes = source_diff.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ReleaseBundleError("stable promotion source diff must not be empty")
    changes: list[dict[str, str]] = []
    observed_paths: set[str] = set()
    for index, raw_change in enumerate(raw_changes):
        change = _require_exact_keys(
            raw_change,
            {"path", "status"},
            owner=f"stable promotion source change {index}",
        )
        path = change.get("path")
        status = change.get("status")
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or path in observed_paths
        ):
            raise ReleaseBundleError("stable promotion source diff contains an unsafe path")
        path_kind = _promotion_path_kind(path)
        if path_kind is None or status not in {"A", "M"}:
            raise ReleaseBundleError(
                "stable promotion source diff changes non-release source"
            )
        if path_kind != "documentation" and status != "M":
            raise ReleaseBundleError(
                "stable promotion version metadata must already exist in the candidate"
            )
        observed_paths.add(path)
        changes.append({"path": path, "status": status})
    if changes != sorted(changes, key=lambda change: change["path"]):
        raise ReleaseBundleError(
            "stable promotion source diff changes must be sorted by path"
        )
    return {"digest": source_diff["digest"], "changes": changes}


def _promotion_git_bytes(*arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            check=True,
            cwd=ROOT,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseBundleError(
            "stable promotion source history could not be observed"
        ) from error
    return completed.stdout


def _promotion_git_blob(commit: str, path: str) -> bytes:
    return _promotion_git_bytes("show", f"{commit}:{path}")


def _promotion_git_mode(commit: str, path: str) -> str:
    observed = _promotion_git_bytes("ls-tree", "-z", commit, "--", path)
    records = observed.removesuffix(b"\0").split(b"\0") if observed else []
    if len(records) != 1:
        raise ReleaseBundleError(
            f"stable promotion source path is absent or ambiguous: {path}"
        )
    try:
        mode = records[0].split(b" ", 1)[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise ReleaseBundleError(
            f"stable promotion source path has an invalid Git mode: {path}"
        ) from error
    return mode


def _promotion_source_diff(
    *,
    candidate_commit: str,
    final_commit: str,
    final_tree: str,
    candidate_ref: str,
) -> dict[str, object]:
    if candidate_commit == final_commit:
        raise ReleaseBundleError(
            "stable promotion candidate must be a distinct ancestor of the final release"
        )
    try:
        resolved_candidate_ref = _promotion_git_bytes(
            "rev-parse", f"{candidate_ref}^{{commit}}"
        ).decode("ascii").strip()
    except (ReleaseBundleError, UnicodeDecodeError) as error:
        raise ReleaseBundleError(
            "stable promotion candidate ref does not resolve to the evidence commit"
        ) from error
    if resolved_candidate_ref != candidate_commit:
        raise ReleaseBundleError(
            "stable promotion candidate ref does not resolve to the evidence commit"
        )
    if _git_output("rev-parse", f"{final_commit}^{{tree}}") != final_tree:
        raise ReleaseBundleError(
            "stable promotion final commit does not resolve to the release tree"
        )
    try:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate_commit, final_commit],
            check=False,
            cwd=ROOT,
            capture_output=True,
        )
    except OSError as error:
        raise ReleaseBundleError(
            "stable promotion source history could not be observed"
        ) from error
    if ancestor.returncode == 1:
        raise ReleaseBundleError(
            "stable promotion candidate is not an ancestor of the final release"
        )
    if ancestor.returncode != 0:
        raise ReleaseBundleError(
            "stable promotion source history could not be observed"
        )

    raw_diff = _promotion_git_bytes(
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        candidate_commit,
        final_commit,
        "--",
    )
    fields = raw_diff.split(b"\0")
    if not fields or fields[-1] != b"" or (len(fields) - 1) % 2:
        raise ReleaseBundleError("stable promotion Git source diff is malformed")
    changes: list[dict[str, str]] = []
    for index in range(0, len(fields) - 1, 2):
        try:
            status = fields[index].decode("ascii")
            path = fields[index + 1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReleaseBundleError(
                "stable promotion Git source diff contains a non-UTF-8 path or status"
            ) from error
        changes.append({"path": path, "status": status})
    observed = _validate_promotion_source_diff_shape(
        {
            "digest": f"sha256:{_sha256_bytes(raw_diff)}",
            "changes": sorted(changes, key=lambda change: change["path"]),
        }
    )

    candidate_version = _release_version_from_ref(candidate_ref)
    candidate_version_bytes = candidate_version.encode("utf-8")
    final_version_bytes = b"1.0.0"
    beta_classifier = b"Development Status :: 4 - Beta"
    stable_classifier = b"Development Status :: 5 - Production/Stable"
    for change in observed["changes"]:
        path = change["path"]
        path_kind = _promotion_path_kind(path)
        if _promotion_git_mode(final_commit, path) != "100644" or (
            change["status"] == "M"
            and _promotion_git_mode(candidate_commit, path) != "100644"
        ):
            raise ReleaseBundleError(
                f"stable promotion source paths must remain regular non-executable files: {path}"
            )
        if path_kind == "documentation":
            continue
        candidate_data = _promotion_git_blob(candidate_commit, path)
        final_data = _promotion_git_blob(final_commit, path)
        if candidate_version_bytes not in candidate_data:
            raise ReleaseBundleError(
                f"stable promotion version metadata lacks {candidate_version!r}: {path}"
            )
        expected = candidate_data.replace(candidate_version_bytes, final_version_bytes)
        allowed_results = (
            {
                _promoted_testing_cli_snapshot(
                    candidate_data,
                    candidate_version=candidate_version,
                    final_version="1.0.0",
                )
            }
            if path_kind == "testing-cli-snapshot"
            else {expected}
        )
        if path_kind == "pyproject" and beta_classifier in expected:
            allowed_results.add(expected.replace(beta_classifier, stable_classifier))
        if final_data not in allowed_results:
            raise ReleaseBundleError(
                f"stable promotion changes more than version metadata in {path}"
            )
    return observed


def _promotion_report_path(root: Path, value: object, *, owner: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseBundleError(f"{owner} has an invalid path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or not relative.parts
        or relative.parts[0] != "promotion-reports"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReleaseBundleError(f"{owner} has an invalid path")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ReleaseBundleError(f"{owner} path is missing") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ReleaseBundleError(f"{owner} path has an unsafe parent")
    return root.joinpath(*relative.parts)


def _promotion_signature_integrated_time(
    signature_snapshot: FileSnapshot,
) -> datetime:
    owner = "promotion report Sigstore bundle"
    try:
        bundle = _json_from_snapshot(signature_snapshot, owner=owner)
    except ReleaseBundleError:
        raise
    except (ArithmeticError, RecursionError, ValueError) as error:
        raise ReleaseBundleError(f"{owner} could not be parsed safely") from error
    verification_material = bundle.get("verificationMaterial")
    if not isinstance(verification_material, Mapping):
        raise ReleaseBundleError(
            f"{owner} has no valid Rekor verification material"
        )
    tlog_entries = verification_material.get("tlogEntries")
    if not isinstance(tlog_entries, list) or not tlog_entries:
        raise ReleaseBundleError(f"{owner} has no Rekor transparency-log entry")

    integrated_times: set[int] = set()
    for index, entry in enumerate(tlog_entries):
        if not isinstance(entry, Mapping) or "integratedTime" not in entry:
            raise ReleaseBundleError(
                f"{owner} Rekor entry {index} has no integratedTime"
            )
        raw_integrated_time = entry.get("integratedTime")
        if isinstance(raw_integrated_time, bool):
            raise ReleaseBundleError(
                f"{owner} Rekor entry {index} has a malformed integratedTime"
            )
        if isinstance(raw_integrated_time, int):
            integrated_time = raw_integrated_time
        elif isinstance(raw_integrated_time, str) and re.fullmatch(
            r"[1-9][0-9]*", raw_integrated_time
        ):
            try:
                integrated_time = int(raw_integrated_time)
            except ValueError as error:
                raise ReleaseBundleError(
                    f"{owner} Rekor entry {index} has a malformed integratedTime"
                ) from error
        else:
            raise ReleaseBundleError(
                f"{owner} Rekor entry {index} has a malformed integratedTime"
            )
        if integrated_time <= 0:
            raise ReleaseBundleError(
                f"{owner} Rekor entry {index} has a malformed integratedTime"
            )
        integrated_times.add(integrated_time)

    if len(integrated_times) != 1:
        raise ReleaseBundleError(
            f"{owner} contains inconsistent Rekor integratedTime values"
        )
    try:
        return datetime.fromtimestamp(integrated_times.pop(), timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ReleaseBundleError(
            f"{owner} contains an out-of-range Rekor integratedTime"
        ) from error


def _verify_promotion_report_signature(
    *,
    report_snapshot: FileSnapshot,
    signature_snapshot: FileSnapshot,
    certificate_identity: str,
    certificate_oidc_issuer: str,
    expected_certificate_identity: str,
    cosign: str | Sequence[str],
) -> datetime:
    if certificate_identity != expected_certificate_identity:
        raise ReleaseBundleError(
            "promotion report signature identity does not match its trusted attestor"
        )
    if certificate_oidc_issuer != SIGSTORE_ISSUER:
        raise ReleaseBundleError(
            "promotion report signature issuer is not GitHub Actions"
        )
    if _observe_cosign_identity(cosign).get("version") != PINNED_COSIGN_VERSION:
        raise ReleaseBundleError("promotion report signature verifier is not pinned")
    with TemporaryDirectory(prefix="graphblocks-promotion-verify-") as temporary_root:
        frozen_report = Path(temporary_root) / "report.json"
        frozen_signature = Path(temporary_root) / "report.sigstore.json"
        frozen_report.write_bytes(report_snapshot.data)
        frozen_signature.write_bytes(signature_snapshot.data)
        try:
            subprocess.run(
                [
                    *_tool_command(cosign),
                    "verify-blob",
                    str(frozen_report),
                    "--bundle",
                    str(frozen_signature),
                    "--certificate-identity",
                    certificate_identity,
                    "--certificate-oidc-issuer",
                    certificate_oidc_issuer,
                ],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ReleaseBundleError(
                "promotion report signature verification failed"
            ) from error
    return _promotion_signature_integrated_time(signature_snapshot)


def _promotion_report_artifacts(
    value: object,
    *,
    root: Path,
    candidate_ref: str,
    cosign: str | Sequence[str],
) -> tuple[
    dict[str, PromotionReportArtifact],
    tuple[FileSnapshot, ...],
]:
    if not isinstance(value, list) or not value:
        raise ReleaseBundleError("stable promotion evidence has no signed report artifacts")
    reports: dict[str, PromotionReportArtifact] = {}
    snapshots: list[FileSnapshot] = []
    observed_paths: set[str] = set()
    for index, raw_record in enumerate(value):
        owner = f"stable promotion report artifact {index}"
        record = _require_exact_keys(
            raw_record,
            {
                "path",
                "sha256",
                "signaturePath",
                "signatureSha256",
                "certificateIdentity",
                "certificateOidcIssuer",
            },
            owner=owner,
        )
        sha256 = record.get("sha256")
        signature_sha256 = record.get("signatureSha256")
        if (
            not isinstance(sha256, str)
            or CANONICAL_SHA256.fullmatch(sha256) is None
            or not isinstance(signature_sha256, str)
            or CANONICAL_SHA256.fullmatch(signature_sha256) is None
        ):
            raise ReleaseBundleError(f"{owner} has an invalid digest")
        digest = f"sha256:{sha256}"
        if digest in reports:
            raise ReleaseBundleError("stable promotion report digests must be unique")
        report_path = _promotion_report_path(root, record.get("path"), owner=owner)
        signature_path = _promotion_report_path(
            root,
            record.get("signaturePath"),
            owner=f"{owner} signature",
        )
        relative_paths = {
            report_path.relative_to(root).as_posix(),
            signature_path.relative_to(root).as_posix(),
        }
        if len(relative_paths) != 2 or observed_paths & relative_paths:
            raise ReleaseBundleError("stable promotion report artifact paths must be unique")
        observed_paths.update(relative_paths)
        report_snapshot = _snapshot_regular_file(report_path, owner=owner)
        signature_snapshot = _snapshot_regular_file(
            signature_path, owner=f"{owner} signature"
        )
        if (
            report_snapshot.sha256 != sha256
            or signature_snapshot.sha256 != signature_sha256
        ):
            raise ReleaseBundleError(
                "stable promotion report artifact digest does not match its file"
            )
        report_payload = _json_from_snapshot(report_snapshot, owner=owner)
        if report_snapshot.data != _canonical_json_bytes(report_payload):
            raise ReleaseBundleError(
                "stable promotion report artifacts must use canonical JSON formatting"
            )
        certificate_identity = _require_nonempty_string(
            record.get("certificateIdentity"), owner=f"{owner} certificate identity"
        )
        certificate_oidc_issuer = _require_nonempty_string(
            record.get("certificateOidcIssuer"), owner=f"{owner} certificate issuer"
        )
        trusted_workflow = (
            SIGSTORE_WORKFLOW
            if set(report_payload) == MATRIX_PROMOTION_REPORT_KEYS
            else PROMOTION_SIGSTORE_WORKFLOW
        )
        candidate_workflow_identity = (
            f"https://github.com/{SIGSTORE_REPOSITORY}/"
            f"{trusted_workflow}@"
            f"{candidate_ref}"
        )
        signature_integrated_at = _verify_promotion_report_signature(
            report_snapshot=report_snapshot,
            signature_snapshot=signature_snapshot,
            certificate_identity=certificate_identity,
            certificate_oidc_issuer=certificate_oidc_issuer,
            expected_certificate_identity=candidate_workflow_identity,
            cosign=cosign,
        )
        reports[digest] = PromotionReportArtifact(
            report_payload,
            report_snapshot,
            signature_snapshot,
            signature_integrated_at,
        )
        snapshots.extend((report_snapshot, signature_snapshot))
    return reports, tuple(snapshots)


def _promotion_report_artifact(
    reports: Mapping[str, PromotionReportArtifact],
    digest: str,
    *,
    owner: str,
) -> PromotionReportArtifact:
    artifact = reports.get(digest)
    if artifact is None:
        raise ReleaseBundleError(f"{owner} does not resolve to a signed report artifact")
    return artifact


def _promotion_report(
    reports: Mapping[str, PromotionReportArtifact],
    digest: str,
    *,
    owner: str,
) -> dict[str, object]:
    return _promotion_report_artifact(reports, digest, owner=owner).payload


def _candidate_manifest_report(
    *,
    candidate_ref: str,
    candidate_commit: str,
) -> dict[str, object]:
    return {
        "formatVersion": 1,
        "releaseRef": candidate_ref,
        "releaseVersion": _release_version_from_ref(candidate_ref),
        "gitCommit": candidate_commit,
    }


def _write_frozen_promotion_report(
    payload: Mapping[str, object],
    *,
    output_dir: Path,
    owner: str,
) -> FileSnapshot:
    if output_dir.is_symlink() or output_dir.exists():
        raise ReleaseBundleError("frozen promotion report output must not already exist")
    try:
        output_dir.mkdir(parents=True)
        output_path = output_dir / "report.json"
        _write_json(output_path, payload)
    except OSError as error:
        raise ReleaseBundleError("frozen promotion report could not be written") from error
    return _snapshot_regular_file(output_path, owner=owner)


def freeze_candidate_matrix_report(
    *,
    output_dir: Path,
    candidate_ref: str,
    candidate_commit: str,
    run_id: str,
) -> FileSnapshot:
    """Freeze one successful matrix claim from the gated candidate CI run."""

    if RELEASE_CANDIDATE_REF.fullmatch(candidate_ref) is None:
        raise ReleaseBundleError(
            "matrix reports must be frozen from a canonical release-candidate ref"
        )
    if GIT_COMMIT.fullmatch(candidate_commit) is None:
        raise ReleaseBundleError("matrix report candidate commit is invalid")
    canonical_run_id = _require_ci_run_attempt_id(run_id, owner="matrix run id")
    candidate_manifest = _candidate_manifest_report(
        candidate_ref=candidate_ref,
        candidate_commit=candidate_commit,
    )
    payload: dict[str, object] = {
        "runId": canonical_run_id,
        "status": "success",
        "complete": True,
        "candidateRef": candidate_ref,
        "candidateCommit": candidate_commit,
        "candidateManifestDigest": "sha256:"
        + _sha256_bytes(_canonical_json_bytes(candidate_manifest)),
        "supportedMatrix": [
            {"os": os_name, "python": python_version}
            for os_name, python_version in SUPPORTED_PLATFORM_MATRIX
        ],
    }
    return _write_frozen_promotion_report(
        payload,
        output_dir=output_dir,
        owner="frozen candidate matrix report",
    )


def freeze_promotion_report(
    *,
    input_path: Path,
    output_dir: Path,
    report_type: str,
    candidate_ref: str,
    candidate_commit: str,
) -> FileSnapshot:
    """Validate and canonically freeze one report before entering the OIDC job."""

    if report_type not in PROMOTION_REPORT_TYPES:
        raise ReleaseBundleError("promotion report type is not supported")
    if RELEASE_CANDIDATE_REF.fullmatch(candidate_ref) is None:
        raise ReleaseBundleError(
            "promotion reports must be frozen from a canonical release-candidate ref"
        )
    if GIT_COMMIT.fullmatch(candidate_commit) is None:
        raise ReleaseBundleError("promotion report candidate commit is invalid")

    owner = f"{report_type} promotion report"
    input_snapshot = _snapshot_regular_file(input_path, owner=owner)
    payload = _json_from_snapshot(input_snapshot, owner=owner)
    if report_type == "candidate-manifest":
        if payload != _candidate_manifest_report(
            candidate_ref=candidate_ref,
            candidate_commit=candidate_commit,
        ):
            raise ReleaseBundleError(
                "candidate manifest promotion report does not bind this candidate"
            )
    elif report_type == "soak-application":
        report = _require_exact_keys(
            payload,
            {"applicationId", "nontrivial", "startedAt", "endedAt"},
            owner=owner,
        )
        _require_nonempty_string(
            report.get("applicationId"), owner="soak application id"
        )
        started_at = _parse_utc_timestamp(
            report.get("startedAt"), owner="soak application start"
        )
        ended_at = _parse_utc_timestamp(
            report.get("endedAt"), owner="soak application end"
        )
        if (
            report.get("nontrivial") is not True
            or started_at >= ended_at
            or ended_at > datetime.now(timezone.utc)
            or (ended_at - started_at).total_seconds() < 14 * 24 * 60 * 60
        ):
            raise ReleaseBundleError(
                "soak application promotion report is not a completed 14-day nontrivial soak"
            )
    elif report_type in {"api-review", "security-review"}:
        report = _require_exact_keys(
            payload,
            {"reviewerIdentity", "approved", "candidateRef", "candidateCommit"},
            owner=owner,
        )
        _require_nonempty_string(
            report.get("reviewerIdentity"), owner=f"{report_type} reviewer identity"
        )
        if (
            report.get("approved") is not True
            or report.get("candidateRef") != candidate_ref
            or report.get("candidateCommit") != candidate_commit
        ):
            raise ReleaseBundleError(
                f"{report_type} promotion report does not approve this candidate"
            )
    elif report_type == "stable-scope":
        report = _require_exact_keys(
            payload,
            {"unresolvedCritical", "unresolvedHigh", "unexplainedFlakes"},
            owner=owner,
        )
        if any(
            not isinstance(report.get(name), int)
            or isinstance(report.get(name), bool)
            or report.get(name) != 0
            for name in ("unresolvedCritical", "unresolvedHigh", "unexplainedFlakes")
        ):
            raise ReleaseBundleError(
                "stable-scope promotion report contains unresolved release blockers"
            )
    elif report_type == "protected-final-ref":
        report = _require_exact_keys(
            payload,
            {"releaseRef", "protected"},
            owner=owner,
        )
        if report != {"releaseRef": "refs/tags/v1.0.0", "protected": True}:
            raise ReleaseBundleError(
                "protected-ref promotion report does not bind the final release ref"
            )
    else:
        report = _require_exact_keys(
            payload,
            {
                "environment",
                "authorized",
                "realExternalActions",
                "authorizedBy",
                "operations",
            },
            owner=owner,
        )
        _require_nonempty_string(
            report.get("authorizedBy"), owner="staged rehearsal authorizer"
        )
        operations = report.get("operations")
        if not isinstance(operations, list) or len(operations) != 4:
            raise ReleaseBundleError(
                "staged rehearsal promotion report must contain four operations"
            )
        observed_operations: dict[str, str] = {}
        for index, raw_operation in enumerate(operations):
            operation = _require_exact_keys(
                raw_operation,
                {"operation", "status"},
                owner=f"staged rehearsal operation {index}",
            )
            name = _require_nonempty_string(
                operation.get("operation"),
                owner=f"staged rehearsal operation {index} name",
            )
            status = _require_nonempty_string(
                operation.get("status"),
                owner=f"staged rehearsal operation {index} status",
            )
            if name in observed_operations:
                raise ReleaseBundleError(
                    "staged rehearsal promotion report contains duplicate operations"
                )
            observed_operations[name] = status
        if (
            report.get("environment") != "staging"
            or report.get("authorized") is not True
            or report.get("realExternalActions") is not True
            or observed_operations
            != {
                "publish": "success",
                "rollback": "success",
                "yank": "success",
                "restore": "success",
            }
        ):
            raise ReleaseBundleError(
                "staged rehearsal promotion report is not an authorized real rehearsal"
            )

    return _write_frozen_promotion_report(
        payload,
        output_dir=output_dir,
        owner="frozen promotion report",
    )


def _validate_promotion_evidence(
    snapshot: FileSnapshot,
    *,
    git_commit: str,
    git_tree: str,
    release_ref: str,
    release_version: str,
    verify_source_diff: bool,
    cosign: str | Sequence[str] = "cosign",
) -> tuple[dict[str, object], str, tuple[FileSnapshot, ...]]:
    owner = "stable promotion evidence"
    payload = _json_from_snapshot(snapshot, owner=owner)
    if snapshot.data != _canonical_json_bytes(payload):
        raise ReleaseBundleError(
            "stable promotion evidence must use canonical JSON formatting"
        )
    _require_exact_keys(
        payload,
        {
            "formatVersion",
            "release",
            "candidate",
            "upgradeGate",
            "supportedMatrixRuns",
            "soak",
            "reviews",
            "stableScope",
            "protectedFinalRef",
            "stagedRehearsal",
            "reportArtifacts",
            "contentDigest",
        },
        owner=owner,
    )
    content_digest = _require_content_digest(payload, owner=owner)
    if payload.get("formatVersion") != 1:
        raise ReleaseBundleError("stable promotion evidence formatVersion must be 1")

    release = _require_exact_keys(
        payload.get("release"),
        {"releaseRef", "releaseVersion"},
        owner="stable promotion release binding",
    )
    if release != {
        "releaseRef": release_ref,
        "releaseVersion": release_version,
    }:
        raise ReleaseBundleError(
            "stable promotion evidence does not bind the exact final ref and version"
        )

    upgrade_gate = _require_exact_keys(
        payload.get("upgradeGate"),
        {"status", "reason"},
        owner="stable promotion upgrade gate",
    )
    if upgrade_gate != {
        "status": "not-applicable",
        "reason": "first-stable-release",
    }:
        raise ReleaseBundleError(
            "v1.0.0 promotion requires the explicit first-stable upgrade exemption"
        )

    candidate = _require_exact_keys(
        payload.get("candidate"),
        {"releaseRef", "gitCommit", "manifestDigest", "sourceDiff"},
        owner="stable promotion candidate binding",
    )
    candidate_ref = candidate.get("releaseRef")
    candidate_commit = candidate.get("gitCommit")
    candidate_manifest_digest = _require_prefixed_sha256(
        candidate.get("manifestDigest"),
        owner="stable promotion candidate manifest digest",
    )
    source_diff = _validate_promotion_source_diff_shape(candidate.get("sourceDiff"))
    if (
        not isinstance(candidate_ref, str)
        or RELEASE_CANDIDATE_REF.fullmatch(candidate_ref) is None
        or not isinstance(candidate_commit, str)
        or GIT_COMMIT.fullmatch(candidate_commit) is None
        or candidate_commit == git_commit
    ):
        raise ReleaseBundleError(
            "stable promotion evidence must bind a canonical distinct prior release candidate"
        )
    if verify_source_diff:
        observed_source_diff = _promotion_source_diff(
            candidate_commit=candidate_commit,
            final_commit=git_commit,
            final_tree=git_tree,
            candidate_ref=candidate_ref,
        )
        if source_diff != observed_source_diff:
            raise ReleaseBundleError(
                "stable promotion source diff does not match the candidate and final commits"
            )
    reports, report_snapshots = _promotion_report_artifacts(
        payload.get("reportArtifacts"),
        root=snapshot.path.parent,
        candidate_ref=candidate_ref,
        cosign=cosign,
    )
    candidate_manifest = _promotion_report(
        reports,
        candidate_manifest_digest,
        owner="stable promotion candidate manifest",
    )
    if candidate_manifest != _candidate_manifest_report(
        candidate_ref=candidate_ref,
        candidate_commit=candidate_commit,
    ):
        raise ReleaseBundleError(
            "stable promotion candidate manifest does not bind the prior candidate"
        )
    used_report_digests = {candidate_manifest_digest}

    matrix_runs = payload.get("supportedMatrixRuns")
    if not isinstance(matrix_runs, list) or len(matrix_runs) < 3:
        raise ReleaseBundleError(
            "stable promotion requires at least three supported-matrix run attestations"
        )
    expected_matrix = [
        {"os": os_name, "python": python_version}
        for os_name, python_version in SUPPORTED_PLATFORM_MATRIX
    ]
    run_ids: set[str] = set()
    run_digests: set[str] = set()
    for index, raw_run in enumerate(matrix_runs):
        run = _require_exact_keys(
            raw_run,
            {
                "runId",
                "status",
                "complete",
                "candidateRef",
                "candidateCommit",
                "candidateManifestDigest",
                "supportedMatrix",
                "attestationDigest",
            },
            owner=f"stable promotion matrix run {index}",
        )
        run_id = _require_ci_run_attempt_id(
            run.get("runId"), owner=f"stable promotion matrix run {index} id"
        )
        attestation_digest = _require_prefixed_sha256(
            run.get("attestationDigest"),
            owner=f"stable promotion matrix run {index} attestation digest",
        )
        _require_prefixed_sha256(
            run.get("candidateManifestDigest"),
            owner=f"stable promotion matrix run {index} candidate manifest digest",
        )
        if (
            run.get("status") != "success"
            or run.get("complete") is not True
            or run.get("candidateRef") != candidate_ref
            or run.get("candidateCommit") != candidate_commit
            or run.get("candidateManifestDigest") != candidate_manifest_digest
            or run.get("supportedMatrix") != expected_matrix
        ):
            raise ReleaseBundleError(
                "stable promotion matrix run is incomplete or does not bind the prior candidate"
            )
        if run_id in run_ids or attestation_digest in run_digests:
            raise ReleaseBundleError(
                "stable promotion matrix runs must have distinct ids and attestations"
            )
        run_ids.add(run_id)
        run_digests.add(attestation_digest)
        run_report = _promotion_report(
            reports,
            attestation_digest,
            owner=f"stable promotion matrix run {index}",
        )
        if run_report != {
            key: value for key, value in run.items() if key != "attestationDigest"
        }:
            raise ReleaseBundleError(
                "stable promotion matrix report does not bind its run attestation"
            )
        used_report_digests.add(attestation_digest)

    soak = _require_exact_keys(
        payload.get("soak"),
        {"startedAt", "endedAt", "applications"},
        owner="stable promotion soak",
    )
    started_at = _parse_utc_timestamp(
        soak.get("startedAt"), owner="stable promotion soak start"
    )
    ended_at = _parse_utc_timestamp(
        soak.get("endedAt"), owner="stable promotion soak end"
    )
    if started_at >= ended_at or ended_at > datetime.now(timezone.utc):
        raise ReleaseBundleError(
            "stable promotion soak must be complete and must not end in the future"
        )
    if (ended_at - started_at).total_seconds() < 14 * 24 * 60 * 60:
        raise ReleaseBundleError("stable promotion soak must be at least 14 days")
    applications = soak.get("applications")
    if not isinstance(applications, list) or len(applications) < 2:
        raise ReleaseBundleError(
            "stable promotion soak requires at least two nontrivial applications"
        )
    application_ids: set[str] = set()
    application_digests: set[str] = set()
    for index, raw_application in enumerate(applications):
        application = _require_exact_keys(
            raw_application,
            {"applicationId", "nontrivial", "reportDigest"},
            owner=f"stable promotion soak application {index}",
        )
        application_id = _require_nonempty_string(
            application.get("applicationId"),
            owner=f"stable promotion soak application {index} id",
        )
        report_digest = _require_prefixed_sha256(
            application.get("reportDigest"),
            owner=f"stable promotion soak application {index} report digest",
        )
        if application.get("nontrivial") is not True:
            raise ReleaseBundleError(
                "stable promotion soak applications must be attested as nontrivial"
            )
        if application_id in application_ids or report_digest in application_digests:
            raise ReleaseBundleError(
                "stable promotion soak applications must be distinct"
            )
        application_ids.add(application_id)
        application_digests.add(report_digest)
        application_artifact = _promotion_report_artifact(
            reports,
            report_digest,
            owner=f"stable promotion soak application {index}",
        )
        application_report = _require_exact_keys(
            application_artifact.payload,
            {"applicationId", "nontrivial", "startedAt", "endedAt"},
            owner=f"stable promotion soak application {index} report",
        )
        application_started_at = _parse_utc_timestamp(
            application_report.get("startedAt"),
            owner=f"stable promotion soak application {index} report start",
        )
        application_ended_at = _parse_utc_timestamp(
            application_report.get("endedAt"),
            owner=f"stable promotion soak application {index} report end",
        )
        if (
            application_report.get("applicationId") != application_id
            or application_report.get("nontrivial") is not True
            or application_started_at != started_at
            or application_ended_at != ended_at
        ):
            raise ReleaseBundleError(
                "stable promotion soak application report does not cover the soak period"
            )
        if application_artifact.signature_integrated_at < application_ended_at:
            raise ReleaseBundleError(
                "stable promotion soak application report was signed before its claimed end"
            )
        used_report_digests.add(report_digest)

    reviews = _require_exact_keys(
        payload.get("reviews"),
        {"api", "security"},
        owner="stable promotion reviews",
    )
    reviewer_identities: set[str] = set()
    review_digests: set[str] = set()
    for review_name in ("api", "security"):
        review = _require_exact_keys(
            reviews.get(review_name),
            {"reviewerIdentity", "approved", "reportDigest"},
            owner=f"stable promotion {review_name} review",
        )
        reviewer = _require_nonempty_string(
            review.get("reviewerIdentity"),
            owner=f"stable promotion {review_name} reviewer identity",
        )
        report_digest = _require_prefixed_sha256(
            review.get("reportDigest"),
            owner=f"stable promotion {review_name} review report digest",
        )
        if review.get("approved") is not True:
            raise ReleaseBundleError(
                f"stable promotion {review_name} review is not approved"
            )
        if reviewer in reviewer_identities or report_digest in review_digests:
            raise ReleaseBundleError(
                "stable promotion API and security reviews must be independent"
            )
        reviewer_identities.add(reviewer)
        review_digests.add(report_digest)
        review_report = _promotion_report(
            reports,
            report_digest,
            owner=f"stable promotion {review_name} review",
        )
        if review_report != {
            "reviewerIdentity": reviewer,
            "approved": True,
            "candidateRef": candidate_ref,
            "candidateCommit": candidate_commit,
        }:
            raise ReleaseBundleError(
                f"stable promotion {review_name} report does not bind the candidate review"
            )
        used_report_digests.add(report_digest)

    stable_scope = _require_exact_keys(
        payload.get("stableScope"),
        {
            "unresolvedCritical",
            "unresolvedHigh",
            "unexplainedFlakes",
            "reportDigest",
        },
        owner="stable promotion stable-scope defect status",
    )
    if any(
        not isinstance(stable_scope.get(name), int)
        or isinstance(stable_scope.get(name), bool)
        or stable_scope.get(name) != 0
        for name in ("unresolvedCritical", "unresolvedHigh", "unexplainedFlakes")
    ):
        raise ReleaseBundleError(
            "stable promotion requires zero unresolved critical/high defects and "
            "unexplained flakes"
        )
    stable_scope_report_digest = _require_prefixed_sha256(
        stable_scope.get("reportDigest"),
        owner="stable promotion stable-scope report digest",
    )
    if _promotion_report(
        reports,
        stable_scope_report_digest,
        owner="stable promotion stable-scope defect status",
    ) != {
        key: value for key, value in stable_scope.items() if key != "reportDigest"
    }:
        raise ReleaseBundleError(
            "stable promotion stable-scope report does not bind defect status"
        )
    used_report_digests.add(stable_scope_report_digest)

    protected_ref = _require_exact_keys(
        payload.get("protectedFinalRef"),
        {"releaseRef", "protected", "reportDigest"},
        owner="stable promotion protected final ref",
    )
    protected_ref_digest = _require_prefixed_sha256(
        protected_ref.get("reportDigest"),
        owner="stable promotion protected final ref report digest",
    )
    if protected_ref.get("releaseRef") != release_ref or protected_ref.get(
        "protected"
    ) is not True:
        raise ReleaseBundleError("stable promotion final ref is not attested as protected")
    if _promotion_report(
        reports,
        protected_ref_digest,
        owner="stable promotion protected final ref",
    ) != {"releaseRef": release_ref, "protected": True}:
        raise ReleaseBundleError(
            "stable promotion protected final ref report does not bind the final ref"
        )
    used_report_digests.add(protected_ref_digest)

    rehearsal = _require_exact_keys(
        payload.get("stagedRehearsal"),
        {
            "environment",
            "authorized",
            "realExternalActions",
            "authorizedBy",
            "reportDigest",
            "operations",
        },
        owner="stable promotion staged rehearsal",
    )
    _require_nonempty_string(
        rehearsal.get("authorizedBy"),
        owner="stable promotion staged rehearsal authorizer",
    )
    rehearsal_report_digest = _require_prefixed_sha256(
        rehearsal.get("reportDigest"),
        owner="stable promotion staged rehearsal report digest",
    )
    operations = rehearsal.get("operations")
    if not isinstance(operations, list) or len(operations) != 4:
        raise ReleaseBundleError(
            "stable promotion staged rehearsal must cover exactly four recovery operations"
        )
    observed_operations: dict[str, str] = {}
    for index, raw_operation in enumerate(operations):
        operation = _require_exact_keys(
            raw_operation,
            {"operation", "status"},
            owner=f"stable promotion staged rehearsal operation {index}",
        )
        name = _require_nonempty_string(
            operation.get("operation"),
            owner=f"stable promotion staged rehearsal operation {index} name",
        )
        status = _require_nonempty_string(
            operation.get("status"),
            owner=f"stable promotion staged rehearsal operation {index} status",
        )
        if name in observed_operations:
            raise ReleaseBundleError(
                "stable promotion staged rehearsal contains duplicate operations"
            )
        observed_operations[name] = status
    if (
        rehearsal.get("environment") != "staging"
        or rehearsal.get("authorized") is not True
        or rehearsal.get("realExternalActions") is not True
        or observed_operations
        != {
            "publish": "success",
            "rollback": "success",
            "yank": "success",
            "restore": "success",
        }
    ):
        raise ReleaseBundleError(
            "stable promotion requires an authorized real staged "
            "publish/rollback/yank/restore rehearsal"
        )
    if _promotion_report(
        reports,
        rehearsal_report_digest,
        owner="stable promotion staged rehearsal",
    ) != {key: value for key, value in rehearsal.items() if key != "reportDigest"}:
        raise ReleaseBundleError(
            "stable promotion staged rehearsal report does not bind the rehearsal"
        )
    used_report_digests.add(rehearsal_report_digest)
    if used_report_digests != set(reports):
        raise ReleaseBundleError(
            "stable promotion evidence contains unreferenced signed report artifacts"
        )
    return payload, content_digest, report_snapshots


def _release_expectations_snapshot(
    *,
    git_commit: str,
    git_tree: str,
    release_ref: str,
    release_version: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "formatVersion": 1,
        "source": {
            "gitCommit": git_commit,
            "gitTree": git_tree,
            "releaseRef": release_ref,
            "releaseVersion": release_version,
        },
        "expectations": release_evidence_expectations(ROOT),
    }
    payload["contentDigest"] = canonical_hash(payload)
    return payload


def _frozen_release_expectations(
    payload: Mapping[str, object],
    *,
    git_commit: str,
    git_tree: str,
    release_ref: str,
    release_version: str,
) -> tuple[Mapping[str, object], str]:
    digest = _require_content_digest(payload, owner="release expectations")
    if payload.get("formatVersion") != 1 or payload.get("source") != {
        "gitCommit": git_commit,
        "gitTree": git_tree,
        "releaseRef": release_ref,
        "releaseVersion": release_version,
    }:
        raise ReleaseBundleError(
            "release expectations do not bind the release source commit and tree"
        )
    expectations = payload.get("expectations")
    if not isinstance(expectations, Mapping) or set(expectations) != {
        "TCK",
        "acceptance",
    }:
        raise ReleaseBundleError("release expectations snapshot is invalid")
    if not all(isinstance(expectations.get(name), Mapping) for name in expectations):
        raise ReleaseBundleError("release expectations snapshot is invalid")
    return expectations, digest


def _validate_evidence(snapshot: FileSnapshot) -> tuple[dict[str, object], str]:
    payload = _json_from_snapshot(
        snapshot,
        owner=f"release evidence {snapshot.path.name!r}",
    )
    if payload.get("ok") is not True:
        raise ReleaseBundleError(f"release evidence {snapshot.path.name!r} did not pass")
    return payload, _require_content_digest(
        payload,
        owner=f"release evidence {snapshot.path.name!r}",
    )


def _sbom_component_candidates(
    payload: Mapping[str, object],
) -> tuple[tuple[Mapping[str, object], bool], ...]:
    if payload.get("bomFormat") != "CycloneDX":
        raise ReleaseBundleError("SBOM must use CycloneDX JSON")
    if payload.get("specVersion") not in {"1.6", "1.7"}:
        raise ReleaseBundleError("SBOM must use CycloneDX 1.6 or 1.7")
    raw_components = payload.get("components")
    if not isinstance(raw_components, list):
        raise ReleaseBundleError("SBOM must contain a component list")
    candidates: list[tuple[Mapping[str, object], bool]] = []
    for component in raw_components:
        if not isinstance(component, Mapping):
            raise ReleaseBundleError("SBOM contains a malformed component")
        candidates.append((component, False))
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ReleaseBundleError("SBOM contains malformed metadata")
    if isinstance(metadata, Mapping) and "component" in metadata:
        metadata_component = metadata.get("component")
        if not isinstance(metadata_component, Mapping):
            raise ReleaseBundleError("SBOM contains a malformed metadata component")
        candidates.append((metadata_component, True))
    return tuple(candidates)


def _artifact_type(filename: str) -> str:
    if filename.endswith(".whl"):
        return "wheel"
    if filename.endswith(".tar.gz"):
        return "sdist"
    raise ReleaseBundleError(f"unsupported release artifact filename: {filename}")


def _artifact_identity(filename: str) -> tuple[str, str]:
    artifact_type = _artifact_type(filename)
    try:
        if artifact_type == "wheel":
            distribution, version, _build, _tags = parse_wheel_filename(filename)
        else:
            distribution, version = parse_sdist_filename(filename)
    except ValueError as error:
        artifact_label = "wheel" if artifact_type == "wheel" else "PEP 625 sdist"
        raise ReleaseBundleError(
            f"invalid {artifact_label} artifact filename: {filename}"
        ) from error
    return canonicalize_name(str(distribution)), str(version)


def _component_properties(component: Mapping[str, object]) -> dict[str, str]:
    properties = component.get("properties")
    if not isinstance(properties, list):
        return {}
    return {
        str(prop["name"]): str(prop["value"])
        for prop in properties
        if isinstance(prop, dict)
        and isinstance(prop.get("name"), str)
        and isinstance(prop.get("value"), str)
    }


def _sbom_release_artifacts(payload: Mapping[str, object]) -> dict[str, dict[str, str]]:
    raw_components = payload.get("components")
    components = raw_components if isinstance(raw_components, list) else []
    observed: dict[str, dict[str, str]] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        properties = _component_properties(component)
        if properties.get("graphblocks:release-artifact") != "true":
            continue
        filename = component.get("name")
        hashes = component.get("hashes")
        sha256_values = [
            item.get("content")
            for item in hashes
            if isinstance(item, dict) and item.get("alg") == "SHA-256"
        ] if isinstance(hashes, list) else []
        if (
            not isinstance(filename, str)
            or filename in observed
            or len(sha256_values) != 1
            or not isinstance(sha256_values[0], str)
        ):
            raise ReleaseBundleError("SBOM contains an invalid release-artifact component")
        observed[filename] = {
            "sha256": _require_sha256(
                sha256_values[0],
                owner=f"SBOM artifact {filename!r} digest",
            ),
            "distribution": properties.get("graphblocks:distribution", ""),
            "version": properties.get("graphblocks:version", ""),
            "artifactType": properties.get("graphblocks:artifact-type", ""),
        }
    return observed


def _first_party_runtime_dependencies(
    root: Path = ROOT,
) -> dict[str, set[str]]:
    manifests = (
        root / "pyproject.toml",
        root / "packages" / "graphblocks-runtime" / "pyproject.toml",
        root / "packages" / "graphblocks-testing" / "pyproject.toml",
    )
    try:
        observed: dict[str, set[str]] = {}
        for manifest in manifests:
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
            name = canonicalize_name(str(project["name"]))
            dependencies = project["dependencies"]
            if not isinstance(dependencies, list):
                raise TypeError
            observed[name] = {
                canonicalize_name(Requirement(str(requirement)).name)
                for requirement in dependencies
            }
        if set(observed) != {
            "graphblocks",
            "graphblocks-runtime",
            "graphblocks-testing",
        }:
            raise ValueError
        return observed
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
        InvalidRequirement,
    ) as error:
        raise ReleaseBundleError("first-party runtime dependencies are invalid") from error


def _sbom_component_references(
    payload: Mapping[str, object],
) -> tuple[dict[str, tuple[str, str]], dict[str, set[str]]]:
    references: dict[str, tuple[str, str]] = {}
    references_by_name: dict[str, set[str]] = {}
    for component, _is_metadata_component in _sbom_component_candidates(payload):
        reference = component.get("bom-ref")
        name = component.get("name")
        version = component.get("version")
        if (
            not isinstance(reference, str)
            or not reference
            or reference != reference.strip()
            or any(ord(character) < 32 for character in reference)
        ):
            raise ReleaseBundleError("SBOM contains a malformed component reference")
        if reference in references:
            raise ReleaseBundleError("SBOM contains a duplicate component reference")
        identity = (
            name if isinstance(name, str) else "",
            version if isinstance(version, str) else "",
        )
        references[reference] = identity
        if isinstance(name, str) and name:
            references_by_name.setdefault(canonicalize_name(name), set()).add(reference)
    return references, references_by_name


def _sbom_dependency_graph(payload: Mapping[str, object]) -> dict[str, set[str]]:
    raw_dependencies = payload.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise ReleaseBundleError("SBOM must contain a CycloneDX dependency graph")
    graph: dict[str, set[str]] = {}
    for dependency in raw_dependencies:
        if not isinstance(dependency, dict):
            raise ReleaseBundleError("SBOM contains an invalid dependency relationship")
        reference = dependency.get("ref")
        depends_on = dependency.get("dependsOn", [])
        if (
            not isinstance(reference, str)
            or not reference
            or reference in graph
            or not isinstance(depends_on, list)
            or not all(isinstance(item, str) and item for item in depends_on)
            or len(depends_on) != len(set(depends_on))
        ):
            raise ReleaseBundleError("SBOM contains an invalid dependency relationship")
        graph[reference] = set(depends_on)
    references, _references_by_name = _sbom_component_references(payload)
    known_references = set(references)
    if any(
        reference not in known_references
        or any(dependency not in known_references for dependency in dependencies)
        for reference, dependencies in graph.items()
    ):
        raise ReleaseBundleError("SBOM dependency graph contains a dangling reference")
    return graph


def _validate_sbom(
    payload: Mapping[str, object],
    artifacts: Mapping[str, Mapping[str, object]],
    *,
    installed_distributions: Mapping[str, str] | None = None,
) -> None:
    candidates = _sbom_component_candidates(payload)
    described_versions: dict[str, set[str]] = {}
    for component, _is_metadata_component in candidates:
        name = component.get("name")
        version = component.get("version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            described_versions.setdefault(canonicalize_name(name), set()).add(version)
    missing = sorted(
        f"{name}=={version}"
        for name, version in (_artifact_identity(filename) for filename in artifacts)
        if version not in described_versions.get(name, set())
    )
    if missing:
        raise ReleaseBundleError(
            "SBOM does not describe every release artifact: " + ", ".join(missing)
        )
    observed_artifacts = _sbom_release_artifacts(payload)
    expected_artifacts = {
        filename: {
            "sha256": str(record["sha256"]),
            "distribution": _artifact_identity(filename)[0],
            "version": _artifact_identity(filename)[1],
            "artifactType": _artifact_type(filename),
        }
        for filename, record in artifacts.items()
    }
    if observed_artifacts != expected_artifacts:
        raise ReleaseBundleError(
            "SBOM release-artifact filenames and hashes do not match the release set"
        )
    references, references_by_name = _sbom_component_references(payload)
    runtime_components: dict[str, list[tuple[str, str]]] = {}
    runtime_references: set[str] = set()
    release_artifact_references: set[str] = set()
    document_references: set[str] = set()
    unexpected_runtime_components: list[str] = []
    for component, is_metadata_component in candidates:
        reference = component.get("bom-ref")
        if not isinstance(reference, str):
            raise ReleaseBundleError("SBOM contains a malformed component reference")
        if _component_properties(component).get("graphblocks:release-artifact") == "true":
            if is_metadata_component:
                raise ReleaseBundleError(
                    "SBOM metadata component cannot be a release artifact"
                )
            release_artifact_references.add(reference)
            continue
        name = component.get("name")
        version = component.get("version")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or not isinstance(version, str)
            or not version
            or version != version.strip()
        ):
            raise ReleaseBundleError("SBOM contains a malformed runtime component")
        distribution = canonicalize_name(name)
        if is_metadata_component and component == {
            "type": "application",
            "name": "graphblocks-release-bundle",
            "version": "1.0",
            "bom-ref": "urn:graphblocks:release-bundle:1.0",
        }:
            document_references.add(reference)
            continue
        if installed_distributions is None or distribution in installed_distributions:
            runtime_components.setdefault(distribution, []).append((version, reference))
            runtime_references.add(reference)
            continue
        unexpected_runtime_components.append(f"{distribution}=={version}")

    if installed_distributions is not None:
        closure_mismatches = list(unexpected_runtime_components)
        for name, expected_version in installed_distributions.items():
            observed = runtime_components.get(name, [])
            if len(observed) != 1 or observed[0][0] != expected_version:
                observed_versions = sorted({version for version, _reference in observed})
                closure_mismatches.append(
                    f"{name}=={expected_version} (observed {observed_versions!r})"
                )
        if closure_mismatches:
            raise ReleaseBundleError(
                "SBOM runtime components do not exactly match the installed distribution "
                "closure: " + ", ".join(sorted(closure_mismatches))
            )

    dependency_graph = _sbom_dependency_graph(payload)
    missing_dependency_rows = sorted(runtime_references - set(dependency_graph))
    if missing_dependency_rows:
        raise ReleaseBundleError(
            "SBOM dependency graph omits installed distribution rows: "
            + ", ".join(missing_dependency_rows)
        )
    if any(
        not dependencies <= runtime_references
        for reference, dependencies in dependency_graph.items()
        if reference in runtime_references
    ):
        raise ReleaseBundleError(
            "SBOM runtime dependency rows escape the installed distribution closure"
        )
    if any(dependency_graph.get(reference) for reference in release_artifact_references):
        raise ReleaseBundleError("SBOM release-artifact dependency rows must be empty")
    if document_references and any(
        dependency_graph.get(reference) != release_artifact_references
        for reference in document_references
    ):
        raise ReleaseBundleError(
            "SBOM document component must depend on the exact release-artifact set"
        )
    first_party_dependencies = FIRST_PARTY_RUNTIME_DEPENDENCIES
    required_dependencies = set().union(*first_party_dependencies.values())
    missing_dependencies = sorted(required_dependencies - set(references_by_name))
    if missing_dependencies:
        raise ReleaseBundleError(
            "SBOM omits required runtime dependencies: "
            + ", ".join(missing_dependencies)
        )
    expected_versions = (
        {
            name: installed_distributions[name]
            for name in first_party_dependencies
            if name in installed_distributions
        }
        if installed_distributions is not None
        else {
            distribution: version
            for filename in artifacts
            for distribution, version in (_artifact_identity(filename),)
            if distribution in first_party_dependencies
        }
    )
    for distribution, dependencies in first_party_dependencies.items():
        version = expected_versions.get(distribution)
        matching_refs = {
            reference
            for reference in references_by_name.get(distribution, set())
            if reference in references
            and canonicalize_name(references[reference][0]) == distribution
            and references[reference][1] == version
        }
        if len(matching_refs) != 1 or set(
            references_by_name.get(distribution, set())
        ) != matching_refs:
            raise ReleaseBundleError(
                f"SBOM must contain exactly one {distribution} component at its installed version"
            )
        expected_dependency_refs: set[str] = set()
        for dependency in dependencies:
            dependency_version = (
                installed_distributions.get(dependency)
                if installed_distributions is not None
                else None
            )
            matching_dependency_refs = {
                reference
                for reference in references_by_name.get(dependency, set())
                if dependency_version is None
                or (
                    reference in references
                    and canonicalize_name(references[reference][0]) == dependency
                    and references[reference][1] == dependency_version
                )
            }
            if len(matching_dependency_refs) != 1:
                raise ReleaseBundleError(
                    "SBOM dependency components do not match the installed distribution closure"
                )
            expected_dependency_refs.update(matching_dependency_refs)
        first_party_ref = next(iter(matching_refs))
        if dependency_graph.get(first_party_ref) != expected_dependency_refs:
            raise ReleaseBundleError(
                f"SBOM dependency graph does not contain the exact {distribution} runtime edges"
            )


def _copy_snapshots(
    snapshots: Sequence[FileSnapshot], destination: Path
) -> tuple[FileSnapshot, ...]:
    copied: list[FileSnapshot] = []
    destination.mkdir()
    for snapshot in snapshots:
        target = destination / snapshot.path.name
        if target.exists():
            raise ReleaseBundleError(
                f"duplicate release input filename: {snapshot.path.name}"
            )
        target.write_bytes(snapshot.data)
        copied.append(
            FileSnapshot(target, snapshot.data, snapshot.size, snapshot.sha256)
        )
    return tuple(copied)


def _artifact_checksum_manifest(records: Sequence[Mapping[str, object]]) -> str:
    return "".join(f"{record['sha256']}  {record['path']}\n" for record in records)


class PlatformInput(NamedTuple):
    identity: tuple[str, str]
    artifacts: tuple[FileSnapshot, ...]
    evidence: tuple[FileSnapshot, ...]
    sbom_payload: dict[str, object]
    build_tools: dict[str, str]
    build_environment: dict[str, object]
    installed_distributions: dict[str, str]
    observed_tool_identities: dict[str, dict[str, str]]


def _validate_distribution_records(
    value: object,
    *,
    owner: str,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    if not isinstance(value, list):
        raise ReleaseBundleError(f"{owner} has no resolved distribution closure")
    records: list[dict[str, str]] = []
    observed_names: set[str] = set()
    for index, raw_record in enumerate(value):
        record = _require_exact_keys(
            raw_record,
            {"name", "version"},
            owner=f"{owner} distribution {index}",
        )
        name = record.get("name")
        version = record.get("version")
        if (
            not isinstance(name, str)
            or canonicalize_name(name) != name
            or name in observed_names
            or not isinstance(version, str)
            or not version
        ):
            raise ReleaseBundleError(
                f"{owner} contains an invalid distribution identity"
            )
        observed_names.add(name)
        records.append({"name": name, "version": version})
    if records != sorted(records, key=lambda item: item["name"]):
        raise ReleaseBundleError(f"{owner} distributions are not canonical")
    return records, {item["name"]: item["version"] for item in records}


def _validate_build_environment(
    value: object,
    *,
    identity: tuple[str, str],
) -> dict[str, object]:
    environment = _require_exact_keys(
        value,
        {"python", "platform", "runnerImage", "resolvedDistributions"},
        owner="platform build environment",
    )
    python = _require_exact_keys(
        environment.get("python"),
        {"implementation", "version"},
        owner="platform build Python identity",
    )
    python_version = python.get("version")
    if (
        python.get("implementation") != "CPython"
        or not isinstance(python_version, str)
        or not python_version.startswith(identity[1] + ".")
    ):
        raise ReleaseBundleError(
            "platform build environment does not bind the exact CPython runtime"
        )
    if not isinstance(environment.get("platform"), str) or not str(
        environment["platform"]
    ).strip():
        raise ReleaseBundleError("platform build environment has no platform identity")
    runner_image = _require_exact_keys(
        environment.get("runnerImage"),
        {"name", "version"},
        owner="platform runner image",
    )
    if not all(
        isinstance(runner_image.get(name), str) and str(runner_image[name]).strip()
        for name in ("name", "version")
    ):
        raise ReleaseBundleError("platform build environment has no runner image identity")
    distributions, resolved = _validate_distribution_records(
        environment.get("resolvedDistributions"),
        owner="platform build environment",
    )
    required_tools = {**PINNED_BUILD_TOOLS, "cyclonedx-bom": CYCLONEDX_BOM_VERSION}
    if any(resolved.get(name) != version for name, version in required_tools.items()):
        raise ReleaseBundleError(
            "platform build environment does not contain the pinned release tools"
        )
    return {
        "python": dict(python),
        "platform": environment["platform"],
        "runnerImage": dict(runner_image),
        "resolvedDistributions": distributions,
    }


def _first_party_versions(root: Path = ROOT) -> dict[str, str]:
    manifests = (
        root / "pyproject.toml",
        root / "packages" / "graphblocks-runtime" / "pyproject.toml",
        root / "packages" / "graphblocks-testing" / "pyproject.toml",
    )
    versions: dict[str, str] = {}
    try:
        for manifest in manifests:
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
            versions[canonicalize_name(str(project["name"]))] = str(project["version"])
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseBundleError("first-party package manifests are invalid") from error
    return versions


def _wheel_matches_platform(
    filename: str,
    *,
    distribution: str,
    platform_identity: tuple[str, str],
) -> bool:
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except ValueError as error:
        raise ReleaseBundleError(f"invalid wheel artifact filename: {filename}") from error
    os_name, _python_version = platform_identity
    if distribution != "graphblocks-runtime" and any(
        tag.interpreter.startswith("py3") and tag.platform == "any" for tag in tags
    ):
        return True
    if distribution != "graphblocks-runtime":
        return False
    expected_platform = "win_amd64" if os_name == "windows-latest" else "linux_x86_64"
    return any(
        tag.interpreter == RUNTIME_WHEEL_INTERPRETER
        and tag.abi == "abi3"
        and (
            (expected_platform == "win_amd64" and tag.platform == "win_amd64")
            or (
                expected_platform == "linux_x86_64"
                and (
                    tag.platform == "linux_x86_64"
                    or (
                        "manylinux" in tag.platform
                        and tag.platform.endswith("_x86_64")
                    )
                )
            )
        )
        for tag in tags
    )


def _artifact_matches_platform(
    filename: str,
    *,
    distribution: str,
    platform_identity: tuple[str, str],
) -> bool:
    if _artifact_type(filename) == "sdist":
        return True
    return _wheel_matches_platform(
        filename,
        distribution=distribution,
        platform_identity=platform_identity,
    )


def _validate_release_artifact_names(filenames: Sequence[str]) -> None:
    if len(filenames) != len(set(filenames)):
        raise ReleaseBundleError("release artifact set contains duplicate filenames")
    observed: dict[tuple[str, str], set[str]] = {}
    for filename in filenames:
        distribution, _version = _artifact_identity(filename)
        observed.setdefault((distribution, _artifact_type(filename)), set()).add(filename)
    expected_counts = {
        ("graphblocks", "wheel"): 1,
        ("graphblocks", "sdist"): 1,
        ("graphblocks-testing", "wheel"): 1,
        ("graphblocks-testing", "sdist"): 1,
        ("graphblocks-runtime", "wheel"): SUPPORTED_NATIVE_WHEEL_COUNT,
        ("graphblocks-runtime", "sdist"): 1,
    }
    if set(observed) != set(expected_counts) or any(
        len(observed[key]) != expected_count
        for key, expected_count in expected_counts.items()
    ):
        raise ReleaseBundleError(
            "release artifact union is not the exact supported wheel and sdist set"
        )


def _platform_artifact_records(
    artifacts: Sequence[FileSnapshot],
    *,
    identity: tuple[str, str],
) -> list[dict[str, object]]:
    expected_versions = _first_party_versions()
    records: list[dict[str, object]] = []
    artifact_identities: set[tuple[str, str]] = set()
    for artifact in sorted(artifacts, key=lambda item: item.path.name):
        distribution, version = _artifact_identity(artifact.path.name)
        artifact_type = _artifact_type(artifact.path.name)
        artifact_identity = (distribution, artifact_type)
        if artifact_identity in artifact_identities:
            raise ReleaseBundleError(
                f"platform {identity!r} contains duplicate {artifact_type} for "
                f"distribution {distribution!r}"
            )
        if expected_versions.get(distribution) != version:
            raise ReleaseBundleError(
                f"platform {identity!r} contains an unexpected first-party artifact"
            )
        if not _artifact_matches_platform(
            artifact.path.name,
            distribution=distribution,
            platform_identity=identity,
        ):
            raise ReleaseBundleError(
                f"artifact {artifact.path.name!r} does not match platform {identity!r}"
            )
        artifact_identities.add(artifact_identity)
        records.append(
            {
                "filename": artifact.path.name,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "distribution": distribution,
                "version": version,
                "artifactType": artifact_type,
            }
        )
    expected_identities = {
        (distribution, artifact_type)
        for distribution in expected_versions
        for artifact_type in ("wheel", "sdist")
    }
    if artifact_identities != expected_identities:
        raise ReleaseBundleError(
            f"platform {identity!r} does not contain the exact first-party wheel and sdist set"
        )
    return records


def _platform_input(
    path: Path,
    *,
    expectations: Mapping[str, object],
) -> PlatformInput:
    wheelhouse = path / "platform-wheelhouse"
    sdist_root = path / "platform-sdists"
    evidence_root = path / "platform-evidence"
    if (
        path.is_symlink()
        or not path.is_dir()
        or wheelhouse.is_symlink()
        or not wheelhouse.is_dir()
        or sdist_root.is_symlink()
        or not sdist_root.is_dir()
        or evidence_root.is_symlink()
        or not evidence_root.is_dir()
    ):
        raise ReleaseBundleError(f"platform release input has an invalid layout: {path}")
    wheel_entries = tuple(sorted(wheelhouse.iterdir(), key=lambda item: item.name))
    sdist_entries = tuple(sorted(sdist_root.iterdir(), key=lambda item: item.name))
    if not wheel_entries:
        raise ReleaseBundleError(f"platform release input contains no wheels: {path}")
    if not sdist_entries:
        raise ReleaseBundleError(f"platform release input contains no sdists: {path}")
    artifacts = tuple(
        _snapshot_regular_file(entry, owner="platform release artifact")
        for entry in sorted((*wheel_entries, *sdist_entries), key=lambda item: item.name)
    )
    if any(entry.suffix != ".whl" for entry in wheel_entries):
        raise ReleaseBundleError("platform wheelhouse contains a non-wheel entry")
    if any(not entry.name.endswith(".tar.gz") for entry in sdist_entries):
        raise ReleaseBundleError("platform sdist directory contains a non-sdist entry")
    expected_names = {*EXPECTED_EVIDENCE, "sbom.cdx.json"}
    observed_names = {entry.name for entry in evidence_root.iterdir()}
    if observed_names != expected_names:
        raise ReleaseBundleError("platform evidence input set is incomplete or unexpected")
    snapshots = {
        name: _snapshot_regular_file(
            evidence_root / name,
            owner=f"platform evidence {name!r}",
        )
        for name in sorted(expected_names)
    }
    platform_payload = _json_from_snapshot(
        snapshots["platform.json"],
        owner="platform identity evidence",
    )
    _require_content_digest(platform_payload, owner="platform identity evidence")
    platform = platform_payload.get("platform")
    if not isinstance(platform, dict):
        raise ReleaseBundleError("platform identity evidence has no platform")
    identity = (platform.get("os"), platform.get("python"))
    if (
        not isinstance(identity[0], str)
        or not isinstance(identity[1], str)
        or identity not in SUPPORTED_PLATFORM_MATRIX
    ):
        raise ReleaseBundleError("platform identity evidence names an unsupported platform")
    artifact_records = _platform_artifact_records(artifacts, identity=identity)
    if platform_payload.get("artifacts") != artifact_records:
        raise ReleaseBundleError("platform identity evidence does not bind its exact artifacts")
    expected_build_tools = {
        **PINNED_BUILD_TOOLS,
        "rustc": PINNED_RUSTC_VERSION,
    }
    if platform_payload.get("buildTools") != expected_build_tools:
        raise ReleaseBundleError("platform identity evidence has unpinned build tools")
    build_environment = _validate_build_environment(
        platform_payload.get("buildEnvironment"), identity=identity
    )
    _installed_records, installed_distributions = _validate_distribution_records(
        platform_payload.get("installedDistributions"),
        owner="platform installed environment",
    )
    observed_tool_identities = platform_payload.get("observedToolIdentities")
    if not isinstance(observed_tool_identities, dict) or set(
        observed_tool_identities
    ) != {"rustc"}:
        raise ReleaseBundleError("platform identity evidence has no observed rustc identity")
    rustc_identity = observed_tool_identities.get("rustc")
    if not isinstance(rustc_identity, dict):
        raise ReleaseBundleError("platform identity evidence has no observed rustc identity")
    try:
        expected_rustc_identity = parse_rustc_identity(
            str(rustc_identity.get("output", ""))
        )
    except RuntimeError as error:
        raise ReleaseBundleError("platform identity evidence has invalid rustc identity") from error
    if rustc_identity != expected_rustc_identity:
        raise ReleaseBundleError("platform identity evidence has invalid rustc identity")
    if platform_payload.get("sourceDateEpoch") != "315532800":
        raise ReleaseBundleError("platform identity evidence has a non-reproducible epoch")
    tck_payload = _json_from_snapshot(snapshots["tck.json"], owner="platform TCK evidence")
    acceptance_payload = _json_from_snapshot(
        snapshots["acceptance.json"],
        owner="platform acceptance evidence",
    )
    try:
        validate_release_evidence_payloads(
            tck_payload=tck_payload,
            acceptance_payload=acceptance_payload,
            expectations=expectations,
        )
    except RuntimeError as error:
        raise ReleaseBundleError(f"platform release evidence is invalid: {error}") from error
    if platform_payload.get("evidence") != {
        "tck": tck_payload.get("contentDigest"),
        "acceptance": acceptance_payload.get("contentDigest"),
    }:
        raise ReleaseBundleError("platform identity evidence does not bind conformance evidence")
    tck_expectations = expectations.get("TCK")
    if not isinstance(tck_expectations, Mapping) or platform_payload.get("contracts") != {
        "claimedProfiles": list(tck_expectations.get("claimed_profiles", ())),
        "conformanceProfileCatalogDigest": tck_expectations.get(
            "profile_catalog_digest"
        ),
        "schemaManifestDigest": tck_expectations.get("schema_manifest_digest"),
    }:
        raise ReleaseBundleError(
            "platform identity evidence does not bind stable conformance contracts"
        )
    sbom_payload = _json_from_snapshot(snapshots["sbom.cdx.json"], owner="platform SBOM")
    _validate_sbom(
        sbom_payload,
        {record["filename"]: record for record in artifact_records},
        installed_distributions=installed_distributions,
    )
    return PlatformInput(
        identity=identity,
        artifacts=artifacts,
        evidence=tuple(snapshots[name] for name in EXPECTED_EVIDENCE),
        sbom_payload=sbom_payload,
        build_tools=expected_build_tools,
        build_environment=build_environment,
        installed_distributions=installed_distributions,
        observed_tool_identities={"rustc": expected_rustc_identity},
    )


def _load_platform_inputs(
    root: Path,
    *,
    expectations: Mapping[str, object],
) -> tuple[PlatformInput, ...]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseBundleError("platform input root must be a regular directory")
    inputs = tuple(
        _platform_input(path, expectations=expectations)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
    )
    identities = tuple(platform.identity for platform in inputs)
    if len(identities) != len(set(identities)) or set(identities) != set(
        SUPPORTED_PLATFORM_MATRIX
    ):
        raise ReleaseBundleError("release inputs do not cover the exact supported platform matrix")
    _validate_release_artifact_names(
        tuple(
            sorted(
                {artifact.path.name for platform in inputs for artifact in platform.artifacts}
            )
        )
    )
    return tuple(sorted(inputs, key=lambda item: item.identity))


def _release_artifact_component(
    *,
    filename: str,
    record: Mapping[str, object],
) -> dict[str, object]:
    distribution, version = _artifact_identity(filename)
    return {
        "type": "file",
        "name": filename,
        "bom-ref": f"urn:sha256:{record['sha256']}",
        "hashes": [{"alg": "SHA-256", "content": record["sha256"]}],
        "properties": [
            {"name": "graphblocks:release-artifact", "value": "true"},
            {"name": "graphblocks:distribution", "value": distribution},
            {"name": "graphblocks:version", "value": version},
            {"name": "graphblocks:artifact-type", "value": _artifact_type(filename)},
        ],
    }


def _aggregate_sbom(
    platforms: Sequence[PlatformInput],
    artifact_records: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    dependency_components: dict[bytes, dict[str, object]] = {}
    referenced_components: dict[str, tuple[bytes, dict[str, object]]] = {}
    dependency_graph: dict[str, set[str]] = {}
    for platform in platforms:
        raw_components = platform.sbom_payload.get("components")
        platform_components = list(
            raw_components if isinstance(raw_components, list) else []
        )
        metadata = platform.sbom_payload.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("component"), dict):
            platform_components.append(metadata["component"])
        for component in platform_components:
            if not isinstance(component, dict):
                continue
            if _component_properties(component).get("graphblocks:release-artifact") == "true":
                continue
            encoded = _canonical_json_bytes(component)
            reference = component.get("bom-ref")
            if isinstance(reference, str) and reference:
                previous = referenced_components.get(reference)
                if previous is not None and previous[0] != encoded:
                    raise ReleaseBundleError(
                        "platform SBOMs disagree on a referenced dependency component"
                    )
                referenced_components[reference] = (encoded, component)
            else:
                dependency_components[encoded] = component
        for reference, dependencies in _sbom_dependency_graph(
            platform.sbom_payload
        ).items():
            dependency_graph.setdefault(reference, set()).update(dependencies)
    for encoded, component in referenced_components.values():
        dependency_components[encoded] = component
    release_components = [
        _release_artifact_component(filename=filename, record=artifact_records[filename])
        for filename in sorted(artifact_records)
    ]
    release_references = [str(component["bom-ref"]) for component in release_components]
    for reference in release_references:
        dependency_graph.setdefault(reference, set())
    bundle_reference = "urn:graphblocks:release-bundle:1.0"
    dependency_graph[bundle_reference] = set(release_references)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "graphblocks-release-bundle",
                "version": "1.0",
                "bom-ref": bundle_reference,
            },
            "properties": [
                {"name": "graphblocks:platform-count", "value": str(len(platforms))},
                {
                    "name": "graphblocks:sbom-generator",
                    "value": f"cyclonedx-bom=={CYCLONEDX_BOM_VERSION}",
                },
            ],
        },
        "components": [
            dependency_components[key] for key in sorted(dependency_components)
        ]
        + release_components,
        "dependencies": [
            {
                "ref": reference,
                "dependsOn": sorted(dependency_graph[reference]),
            }
            for reference in sorted(dependency_graph)
        ],
    }


def _provenance_statement(
    *,
    artifact_records: Sequence[Mapping[str, object]],
    evidence_records: Sequence[Mapping[str, object]],
    evidence_content_digests: Mapping[str, str],
    expectations_record: Mapping[str, object],
    expectations_content_digest: str,
    promotion_evidence: Mapping[str, object] | None,
    sbom_record: Mapping[str, object],
    build_environments: Sequence[Mapping[str, object]],
    tool_identities: Mapping[str, str],
    observed_tool_identities: Mapping[str, Mapping[str, str]],
    git_commit: str,
    git_tree: str,
    release_ref: str,
    release_version: str,
    builder_id: str,
    invocation_id: str,
) -> dict[str, object]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "name": record["path"],
                "digest": {"sha256": record["sha256"]},
            }
            for record in artifact_records
        ],
        "predicate": {
            "buildDefinition": {
                "buildType": "https://graphblocks.ai/buildtypes/python-distributions/v1",
                "externalParameters": {
                    "targetRelease": "1.0",
                    "releaseRef": release_ref,
                    "releaseVersion": release_version,
                },
                "internalParameters": {
                    "releaseEvidence": [
                        {
                            "path": record["path"],
                            "sha256": record["sha256"],
                            "contentDigest": evidence_content_digests[str(record["path"])],
                        }
                        for record in evidence_records
                    ],
                    "releaseExpectations": {
                        "path": expectations_record["path"],
                        "sha256": expectations_record["sha256"],
                        "contentDigest": expectations_content_digest,
                    },
                    **(
                        {"promotionEvidence": dict(promotion_evidence)}
                        if promotion_evidence is not None
                        else {}
                    ),
                    "sbom": {
                        "path": sbom_record["path"],
                        "sha256": sbom_record["sha256"],
                    },
                    "buildEnvironments": list(build_environments),
                    "toolIdentities": [
                        {"name": name, "version": tool_identities[name]}
                        for name in sorted(tool_identities)
                    ],
                    "observedToolIdentities": {
                        name: dict(observed_tool_identities[name])
                        for name in sorted(observed_tool_identities)
                    },
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/graphblocks/graphblocks@{git_commit}",
                        "digest": {
                            "gitCommit": git_commit,
                            "gitTree": git_tree,
                        },
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {"invocationId": invocation_id},
            },
        },
    }


def _rehearsal_contract(
    *, artifact_records: Sequence[Mapping[str, object]], git_commit: str
) -> dict[str, object]:
    return {
        "formatVersion": 1,
        "ok": True,
        "mode": "deterministic-dry-run",
        "gitCommit": git_commit,
        "artifacts": [
            {"path": record["path"], "sha256": record["sha256"]}
            for record in artifact_records
        ],
        "transitions": [
            {
                "from": "verified-local-bundle",
                "to": "candidate-staged",
                "operation": "publish",
                "preconditions": ["credentials-present", "signature-verified"],
                "verification": "remote filenames and SHA-256 digests match the signed manifest",
            },
            {
                "from": "candidate-staged",
                "to": "candidate-withdrawn",
                "operation": "rollback-before-promotion",
                "preconditions": ["candidate-not-promoted"],
                "verification": "the stable index was not changed",
            },
            {
                "from": "published",
                "to": "yanked",
                "operation": "yank",
                "preconditions": ["incident-authorized", "all-release-files-selected"],
                "verification": "default dependency resolution no longer selects the release",
            },
            {
                "from": "yanked",
                "to": "published",
                "operation": "restore",
                "preconditions": ["incident-resolved", "release-digests-unchanged"],
                "verification": "all restored files still match the signed manifest",
            },
        ],
        "externalActionsExecuted": False,
        "networkRequests": 0,
        "mutations": 0,
        "externalGates": [
            "release-index-credentials",
            "authorized-release-operator",
            "remote-index-observation",
        ],
    }


def assemble_release_bundle(
    *,
    platform_inputs_dir: Path,
    output_dir: Path,
    git_commit: str,
    release_ref: str,
    builder_id: str,
    invocation_id: str,
    promotion_evidence: Path | None = None,
    cosign: str | Sequence[str] = "cosign",
) -> dict[str, object]:
    if GIT_COMMIT.fullmatch(git_commit) is None:
        raise ReleaseBundleError("git commit must be a full lowercase hexadecimal object id")
    release_version = _release_version_from_ref(release_ref)
    try:
        release_ref_commit = _resolve_git_commit(release_ref)
    except ReleaseBundleError as error:
        raise ReleaseBundleError(
            f"release ref {release_ref!r} does not resolve to a commit"
        ) from error
    if release_ref_commit != git_commit:
        raise ReleaseBundleError(
            f"release ref {release_ref!r} resolves to {release_ref_commit}, "
            f"not requested Git commit {git_commit}"
        )
    distribution_versions = _first_party_versions()
    if _first_party_runtime_dependencies() != {
        name: set(dependencies)
        for name, dependencies in FIRST_PARTY_RUNTIME_DEPENDENCIES.items()
    }:
        raise ReleaseBundleError(
            "first-party runtime dependency policy does not match package manifests"
        )
    for stable_distribution in ("graphblocks", "graphblocks-testing"):
        if distribution_versions.get(stable_distribution) != release_version:
            raise ReleaseBundleError(
                f"{stable_distribution} version does not match release ref {release_ref!r}"
            )
    if not builder_id.strip() or not invocation_id.strip():
        raise ReleaseBundleError("builder id and invocation id must not be empty")
    expected_builder_id = (
        f"https://github.com/{SIGSTORE_REPOSITORY}/{SIGSTORE_WORKFLOW}"
    )
    if builder_id != expected_builder_id:
        raise ReleaseBundleError("builder id is not the pinned GraphBlocks release workflow")
    if git_commit != _current_git_commit():
        raise ReleaseBundleError("requested git commit does not match the checked-out HEAD")
    _assert_clean_source_checkout()
    git_tree = _current_git_tree()
    if GIT_COMMIT.fullmatch(git_tree) is None:
        raise ReleaseBundleError("checked-out Git tree id is invalid")
    promotion_snapshot: FileSnapshot | None = None
    promotion_content_digest: str | None = None
    promotion_report_snapshots: tuple[FileSnapshot, ...] = ()
    cosign_identity = _observe_cosign_identity(cosign)
    if release_ref == "refs/tags/v1.0.0":
        if promotion_evidence is None:
            raise ReleaseBundleError(
                "final v1.0.0 assembly requires explicit stable promotion evidence"
            )
        promotion_snapshot = _snapshot_regular_file(
            promotion_evidence,
            owner="stable promotion evidence",
        )
        (
            _promotion_payload,
            promotion_content_digest,
            promotion_report_snapshots,
        ) = _validate_promotion_evidence(
            promotion_snapshot,
            git_commit=git_commit,
            git_tree=git_tree,
            release_ref=release_ref,
            release_version=release_version,
            verify_source_diff=True,
            cosign=cosign,
        )
    elif promotion_evidence is not None:
        raise ReleaseBundleError(
            "release candidates must not supply stable promotion evidence"
        )
    expectations_payload = _release_expectations_snapshot(
        git_commit=git_commit,
        git_tree=git_tree,
        release_ref=release_ref,
        release_version=release_version,
    )
    frozen_expectations, expectations_content_digest = _frozen_release_expectations(
        expectations_payload,
        git_commit=git_commit,
        git_tree=git_tree,
        release_ref=release_ref,
        release_version=release_version,
    )
    if output_dir.exists():
        if output_dir.is_symlink() or not output_dir.is_dir() or any(output_dir.iterdir()):
            raise ReleaseBundleError("release bundle output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    platforms = _load_platform_inputs(
        platform_inputs_dir,
        expectations=frozen_expectations,
    )
    artifact_inputs: dict[str, FileSnapshot] = {}
    for platform in platforms:
        for snapshot in platform.artifacts:
            existing = artifact_inputs.get(snapshot.path.name)
            if existing is not None and (
                existing.sha256,
                existing.size,
            ) != (snapshot.sha256, snapshot.size):
                raise ReleaseBundleError(
                    f"cross-platform artifact {snapshot.path.name!r} is not deterministic"
                )
            artifact_inputs[snapshot.path.name] = snapshot
    copied_artifacts = _copy_snapshots(
        tuple(artifact_inputs[name] for name in sorted(artifact_inputs)),
        output_dir / "artifacts",
    )
    evidence_output = output_dir / "evidence"
    evidence_output.mkdir()
    copied_evidence: list[FileSnapshot] = []
    for platform in platforms:
        os_name, python_version = platform.identity
        platform_output = evidence_output / f"{os_name}-py{python_version.replace('.', '')}"
        copied_evidence.extend(_copy_snapshots(platform.evidence, platform_output))

    artifact_records = tuple(
        _file_record(snapshot, relative_to=output_dir) for snapshot in copied_artifacts
    )
    artifact_records_by_filename = {
        Path(str(record["path"])).name: record for record in artifact_records
    }
    installed_release_distributions: dict[str, str] = {}
    for platform in platforms:
        for name, version in platform.installed_distributions.items():
            previous = installed_release_distributions.get(name)
            if previous is not None and previous != version:
                raise ReleaseBundleError(
                    "supported platforms disagree on an installed distribution version"
                )
            installed_release_distributions[name] = version
    sbom_payload = _aggregate_sbom(platforms, artifact_records_by_filename)
    _validate_sbom(
        sbom_payload,
        artifact_records_by_filename,
        installed_distributions=installed_release_distributions,
    )
    copied_sbom = output_dir / "SBOM.cdx.json"
    _write_json(copied_sbom, sbom_payload)

    evidence_records = tuple(
        _file_record(snapshot, relative_to=output_dir) for snapshot in copied_evidence
    )
    evidence_content_digests: dict[str, str] = {}
    for snapshot, record in zip(copied_evidence, evidence_records, strict=True):
        payload = _json_from_snapshot(snapshot, owner=f"release evidence {snapshot.path.name!r}")
        content_digest = _require_content_digest(
            payload,
            owner=f"release evidence {snapshot.path.name!r}",
        )
        evidence_content_digests[str(record["path"])] = content_digest
    sbom_record = _file_record(
        _snapshot_regular_file(copied_sbom, owner="aggregate release SBOM"),
        relative_to=output_dir,
    )
    expectations_path = output_dir / EXPECTATIONS_NAME
    _write_json(expectations_path, expectations_payload)
    expectations_record = _file_record(
        _snapshot_regular_file(expectations_path, owner="release expectations"),
        relative_to=output_dir,
    )
    promotion_record: dict[str, object] | None = None
    copied_promotion_reports: list[FileSnapshot] = []
    if promotion_snapshot is not None:
        promotion_path = output_dir / PROMOTION_EVIDENCE_NAME
        promotion_path.write_bytes(promotion_snapshot.data)
        promotion_record = _file_record(
            _snapshot_regular_file(promotion_path, owner="copied stable promotion evidence"),
            relative_to=output_dir,
        )
        for report_snapshot in promotion_report_snapshots:
            relative_path = report_snapshot.path.relative_to(
                promotion_snapshot.path.parent
            )
            report_path = output_dir / relative_path
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_bytes(report_snapshot.data)
            copied_promotion_reports.append(
                _snapshot_regular_file(
                    report_path, owner="copied stable promotion report artifact"
                )
            )

    checksum_path = output_dir / "SHA256SUMS"
    checksum_path.write_text(
        _artifact_checksum_manifest(artifact_records),
        encoding="ascii",
        newline="\n",
    )
    provenance_path = output_dir / "provenance.intoto.json"
    _write_json(
        provenance_path,
        _provenance_statement(
            artifact_records=artifact_records,
            evidence_records=evidence_records,
            evidence_content_digests=evidence_content_digests,
            expectations_record=expectations_record,
            expectations_content_digest=expectations_content_digest,
            promotion_evidence=(
                {
                    **promotion_record,
                    "contentDigest": promotion_content_digest,
                }
                if promotion_record is not None
                and promotion_content_digest is not None
                else None
            ),
            sbom_record=sbom_record,
            build_environments=[
                {
                    "os": platform.identity[0],
                    "python": platform.identity[1],
                    "sourceDateEpoch": "315532800",
                    "buildTools": platform.build_tools,
                    "buildEnvironment": platform.build_environment,
                    "observedToolIdentities": platform.observed_tool_identities,
                }
                for platform in platforms
            ],
            tool_identities=PINNED_RELEASE_TOOLS,
            observed_tool_identities={"cosign": cosign_identity},
            git_commit=git_commit,
            git_tree=git_tree,
            release_ref=release_ref,
            release_version=release_version,
            builder_id=builder_id,
            invocation_id=invocation_id,
        ),
    )
    rehearsal_path = output_dir / "rehearsal.json"
    _write_json(
        rehearsal_path,
        _rehearsal_contract(artifact_records=artifact_records, git_commit=git_commit),
    )

    metadata_records = tuple(
        _file_record(
            _snapshot_regular_file(path, owner=f"release metadata {path.name!r}"),
            relative_to=output_dir,
        )
        for path in (
            checksum_path,
            copied_sbom,
            expectations_path,
            provenance_path,
            rehearsal_path,
            *(
                (output_dir / PROMOTION_EVIDENCE_NAME,)
                if promotion_record is not None
                else ()
            ),
            *(
                tuple(snapshot.path for snapshot in copied_promotion_reports)
                if promotion_record is not None
                else ()
            ),
        )
    )
    if _current_git_commit() != git_commit or _current_git_tree() != git_tree:
        raise ReleaseBundleError("release source identity changed during assembly")
    _assert_clean_source_checkout()
    is_final = release_ref == "refs/tags/v1.0.0"
    manifest = {
        "formatVersion": 1,
        "targetRelease": "1.0",
        "releaseRef": release_ref,
        "releaseVersion": release_version,
        "readiness": (
            "promotion-authorized-signature-required" if is_final else "candidate"
        ),
        "gitCommit": git_commit,
        "gitTree": git_tree,
        "distributionVersions": dict(sorted(distribution_versions.items())),
        "platforms": [
            {"os": os_name, "python": python_version}
            for os_name, python_version in SUPPORTED_PLATFORM_MATRIX
        ],
        "toolIdentities": dict(sorted(PINNED_RELEASE_TOOLS.items())),
        "observedToolIdentities": {"cosign": cosign_identity},
        "artifacts": list(artifact_records),
        "evidence": list(evidence_records),
        "metadata": list(metadata_records),
        **(
            {
                "promotionEvidence": {
                    **promotion_record,
                    "contentDigest": promotion_content_digest,
                }
            }
            if promotion_record is not None and promotion_content_digest is not None
            else {}
        ),
        "signaturePolicy": {
            "mechanism": "sigstore-keyless",
            "oidcIssuer": SIGSTORE_ISSUER,
            "repository": SIGSTORE_REPOSITORY,
            "workflow": SIGSTORE_WORKFLOW,
            "allowedRefPattern": RELEASE_REF_PATTERN,
            "subject": "release-manifest.json",
            "bundle": SIGNATURE_BUNDLE_NAME,
            "requiredForStable": True,
            "status": "signature-required" if is_final else "external-gate-pending",
        },
        "externalGates": (
            ["keyless-signing-identity"]
            if is_final
            else [
                "keyless-signing-identity",
                "release-index-credentials",
                "release-candidate-soak",
                "independent-api-review",
                "independent-security-review",
                "protected-final-ref",
                "authorized-real-staged-rehearsal",
            ]
        ),
    }
    _write_json(output_dir / "release-manifest.json", manifest)
    _verify_release_bundle(bundle_dir=output_dir, require_stable_signature=False)
    return manifest


def _records_by_path(value: object, *, owner: str) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise ReleaseBundleError(f"release manifest {owner} must be a nonempty list")
    records: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ReleaseBundleError(f"release manifest {owner} contains a non-object")
        path = item.get("path")
        if not isinstance(path, str) or not path or path in records:
            raise ReleaseBundleError(f"release manifest {owner} contains an invalid path")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise ReleaseBundleError(f"release manifest {owner} path escapes the bundle")
        _require_sha256(item.get("sha256"), owner=f"release manifest {path!r} digest")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReleaseBundleError(f"release manifest {path!r} size is invalid")
        records[path] = item
    return records


def _verify_records(
    *,
    snapshots: Mapping[str, FileSnapshot],
    records: Mapping[str, Mapping[str, object]],
    owner: str,
) -> None:
    for relative_path, record in records.items():
        snapshot = snapshots.get(relative_path)
        if snapshot is None:
            raise ReleaseBundleError(f"{owner} file is missing: {relative_path}")
        observed = {
            "path": relative_path,
            "sha256": snapshot.sha256,
            "size": snapshot.size,
        }
        if observed != dict(record):
            raise ReleaseBundleError(f"{owner} file does not match manifest: {relative_path}")


def _verify_provenance(
    payload: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    artifacts: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
    evidence_content_digests: Mapping[str, str],
    expectations: Mapping[str, object],
    expectations_content_digest: str,
    promotion_evidence: Mapping[str, object] | None,
    sbom: Mapping[str, object],
    build_environments: Sequence[Mapping[str, object]],
) -> None:
    if payload.get("_type") != "https://in-toto.io/Statement/v1":
        raise ReleaseBundleError("provenance is not an in-toto v1 statement")
    if payload.get("predicateType") != "https://slsa.dev/provenance/v1":
        raise ReleaseBundleError("provenance is not a SLSA provenance v1 statement")
    expected_subject = [
        {"name": path, "digest": {"sha256": record["sha256"]}}
        for path, record in artifacts.items()
    ]
    if payload.get("subject") != expected_subject:
        raise ReleaseBundleError("provenance subjects do not match release artifacts")
    predicate = payload.get("predicate")
    build_definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    if not isinstance(build_definition, dict):
        raise ReleaseBundleError("provenance is missing its build definition")
    if build_definition.get("buildType") != (
        "https://graphblocks.ai/buildtypes/python-distributions/v1"
    ):
        raise ReleaseBundleError("provenance build type is not the release distribution builder")
    if build_definition.get("externalParameters") != {
        "targetRelease": "1.0",
        "releaseRef": manifest.get("releaseRef"),
        "releaseVersion": manifest.get("releaseVersion"),
    }:
        raise ReleaseBundleError("provenance does not bind the release ref and version")
    resolved = build_definition.get("resolvedDependencies")
    expected_commit = manifest.get("gitCommit")
    if (
        not isinstance(resolved, list)
        or len(resolved) != 1
        or not isinstance(resolved[0], dict)
        or resolved[0].get("uri")
        != f"git+https://github.com/graphblocks/graphblocks@{expected_commit}"
        or resolved[0].get("digest")
        != {
            "gitCommit": expected_commit,
            "gitTree": manifest.get("gitTree"),
        }
    ):
        raise ReleaseBundleError("provenance does not bind the release git commit")
    internal = build_definition.get("internalParameters")
    release_evidence = internal.get("releaseEvidence") if isinstance(internal, dict) else None
    if not isinstance(release_evidence, list):
        raise ReleaseBundleError("provenance does not bind release evidence")
    observed_evidence = {
        item.get("path"): item
        for item in release_evidence
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if set(observed_evidence) != set(evidence):
        raise ReleaseBundleError("provenance evidence set does not match release manifest")
    for path, record in evidence.items():
        item = observed_evidence[path]
        if (
            item.get("sha256") != record["sha256"]
            or item.get("contentDigest") != evidence_content_digests[path]
        ):
            raise ReleaseBundleError(f"provenance evidence digest does not match: {path}")
    if not isinstance(internal, dict) or internal.get("releaseExpectations") != {
        "path": expectations["path"],
        "sha256": expectations["sha256"],
        "contentDigest": expectations_content_digest,
    }:
        raise ReleaseBundleError("provenance does not bind frozen release expectations")
    observed_promotion_evidence = (
        internal.get("promotionEvidence") if isinstance(internal, dict) else None
    )
    if observed_promotion_evidence != promotion_evidence:
        raise ReleaseBundleError("provenance does not bind stable promotion evidence")
    if not isinstance(internal, dict) or internal.get("sbom") != {
        "path": sbom["path"],
        "sha256": sbom["sha256"],
    }:
        raise ReleaseBundleError("provenance does not bind the release SBOM")
    if not isinstance(internal, dict) or internal.get("buildEnvironments") != list(
        build_environments
    ):
        raise ReleaseBundleError("provenance does not bind every platform build environment")
    expected_tool_identities = [
        {"name": name, "version": version}
        for name, version in sorted(PINNED_RELEASE_TOOLS.items())
    ]
    if internal.get("toolIdentities") != expected_tool_identities:
        raise ReleaseBundleError("provenance does not bind pinned release tool identities")
    if internal.get("observedToolIdentities") != manifest.get(
        "observedToolIdentities"
    ):
        raise ReleaseBundleError("provenance does not bind observed release tool identities")
    run_details = predicate.get("runDetails") if isinstance(predicate, dict) else None
    builder = run_details.get("builder") if isinstance(run_details, dict) else None
    metadata = run_details.get("metadata") if isinstance(run_details, dict) else None
    if (
        not isinstance(builder, dict)
        or not isinstance(builder.get("id"), str)
        or not builder["id"].strip()
        or not isinstance(metadata, dict)
        or not isinstance(metadata.get("invocationId"), str)
        or not metadata["invocationId"].strip()
    ):
        raise ReleaseBundleError("provenance builder and invocation identities are missing")


def _verify_rehearsal(
    payload: Mapping[str, object],
    *, manifest: Mapping[str, object], artifacts: Mapping[str, Mapping[str, object]]
) -> None:
    if (
        payload.get("ok") is not True
        or payload.get("mode") != "deterministic-dry-run"
        or payload.get("gitCommit") != manifest.get("gitCommit")
        or payload.get("externalActionsExecuted") is not False
        or payload.get("networkRequests") != 0
        or payload.get("mutations") != 0
    ):
        raise ReleaseBundleError("publish/rollback/yank rehearsal is not a safe passing dry run")
    expected = [
        {"path": path, "sha256": record["sha256"]}
        for path, record in artifacts.items()
    ]
    if payload.get("artifacts") != expected:
        raise ReleaseBundleError("release rehearsal does not bind the release artifacts")
    transitions = payload.get("transitions")
    operations = {
        transition.get("operation")
        for transition in transitions
        if isinstance(transition, dict)
    } if isinstance(transitions, list) else set()
    if operations != {"publish", "rollback-before-promotion", "yank", "restore"}:
        raise ReleaseBundleError("release rehearsal does not cover every recovery operation")


def _verify_sigstore_signature(
    *,
    manifest_snapshot: FileSnapshot,
    signature_snapshot: FileSnapshot,
    certificate_identity: str,
    certificate_oidc_issuer: str,
    expected_release_ref: str,
    cosign: str | Sequence[str],
    expected_cosign_identity: Mapping[str, str],
) -> None:
    if not certificate_identity.strip():
        raise ReleaseBundleError("Sigstore certificate identity must not be empty")
    if certificate_oidc_issuer != SIGSTORE_ISSUER:
        raise ReleaseBundleError("Sigstore certificate issuer must be the GitHub Actions issuer")
    identity_prefix = (
        f"https://github.com/{SIGSTORE_REPOSITORY}/{SIGSTORE_WORKFLOW}@"
    )
    if not certificate_identity.startswith(identity_prefix):
        raise ReleaseBundleError(
            "Sigstore certificate identity must name the pinned GraphBlocks workflow"
        )
    release_ref = certificate_identity.removeprefix(identity_prefix)
    if release_ref != expected_release_ref or RELEASE_REF.fullmatch(release_ref) is None:
        raise ReleaseBundleError("Sigstore certificate identity does not match the release ref")
    observed_cosign_identity = _observe_cosign_identity(cosign)
    if observed_cosign_identity != expected_cosign_identity:
        raise ReleaseBundleError(
            "signature verifier Cosign identity does not match release evidence"
        )
    with TemporaryDirectory(prefix="graphblocks-sigstore-verify-") as temporary_root:
        frozen_manifest = Path(temporary_root) / "release-manifest.json"
        frozen_signature = Path(temporary_root) / SIGNATURE_BUNDLE_NAME
        frozen_manifest.write_bytes(manifest_snapshot.data)
        frozen_signature.write_bytes(signature_snapshot.data)
        try:
            subprocess.run(
                [
                    *_tool_command(cosign),
                    "verify-blob",
                    str(frozen_manifest),
                    "--bundle",
                    str(frozen_signature),
                    "--certificate-identity",
                    certificate_identity,
                    "--certificate-oidc-issuer",
                    certificate_oidc_issuer,
                ],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ReleaseBundleError(
                "release manifest signature verification failed"
            ) from error


def _bundle_snapshots(
    bundle_dir: Path,
    *,
    manifest_snapshot: FileSnapshot,
) -> tuple[dict[str, FileSnapshot], set[str]]:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ReleaseBundleError("release bundle root must be a regular directory")
    snapshots: dict[str, FileSnapshot] = {
        "release-manifest.json": manifest_snapshot,
    }
    directories: set[str] = set()
    for path in bundle_dir.rglob("*"):
        relative_path = path.relative_to(bundle_dir).as_posix()
        try:
            file_status = path.lstat()
        except OSError as error:
            raise ReleaseBundleError("release bundle changed during traversal") from error
        if stat.S_ISLNK(file_status.st_mode):
            raise ReleaseBundleError(
                f"release bundle contains a symlink: {relative_path}"
            )
        if stat.S_ISDIR(file_status.st_mode):
            directories.add(relative_path)
            continue
        if not stat.S_ISREG(file_status.st_mode):
            raise ReleaseBundleError(
                f"release bundle contains a non-regular entry: {relative_path}"
            )
        if relative_path == "release-manifest.json":
            continue
        snapshots[relative_path] = _snapshot_regular_file(
            path,
            owner=f"release bundle file {relative_path!r}",
        )
    return snapshots, directories


def _verify_platform_evidence(
    *,
    snapshots: Mapping[str, FileSnapshot],
    artifacts: Mapping[str, Mapping[str, object]],
    expectations: Mapping[str, object],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    artifact_by_filename = {
        Path(path).name: record for path, record in artifacts.items()
    }
    if len(artifact_by_filename) != len(artifacts):
        raise ReleaseBundleError("release manifest contains duplicate artifact filenames")
    covered_artifacts: dict[str, str] = {}
    build_environments: list[dict[str, object]] = []
    installed_release_distributions: dict[str, str] = {}
    for os_name, python_version in SUPPORTED_PLATFORM_MATRIX:
        prefix = f"evidence/{os_name}-py{python_version.replace('.', '')}"
        platform_snapshot = snapshots[f"{prefix}/platform.json"]
        tck_snapshot = snapshots[f"{prefix}/tck.json"]
        acceptance_snapshot = snapshots[f"{prefix}/acceptance.json"]
        platform_payload = _json_from_snapshot(
            platform_snapshot,
            owner=f"platform evidence {os_name}/{python_version}",
        )
        _require_content_digest(
            platform_payload,
            owner=f"platform evidence {os_name}/{python_version}",
        )
        if platform_payload.get("platform") != {
            "os": os_name,
            "python": python_version,
        }:
            raise ReleaseBundleError("retained platform evidence names another platform")
        expected_build_tools = {
            **PINNED_BUILD_TOOLS,
            "rustc": PINNED_RUSTC_VERSION,
        }
        if platform_payload.get("buildTools") != expected_build_tools or (
            platform_payload.get("sourceDateEpoch") != "315532800"
        ):
            raise ReleaseBundleError("retained platform evidence has unpinned build tools")
        build_environment = _validate_build_environment(
            platform_payload.get("buildEnvironment"),
            identity=(os_name, python_version),
        )
        _installed_records, installed_distributions = _validate_distribution_records(
            platform_payload.get("installedDistributions"),
            owner="retained platform installed environment",
        )
        for name, version in installed_distributions.items():
            previous = installed_release_distributions.get(name)
            if previous is not None and previous != version:
                raise ReleaseBundleError(
                    "supported platforms disagree on an installed distribution version"
                )
            installed_release_distributions[name] = version
        observed_tool_identities = platform_payload.get("observedToolIdentities")
        rustc_identity = (
            observed_tool_identities.get("rustc")
            if isinstance(observed_tool_identities, dict)
            else None
        )
        if not isinstance(rustc_identity, dict) or set(
            observed_tool_identities or {}
        ) != {"rustc"}:
            raise ReleaseBundleError("retained platform evidence has no observed rustc identity")
        try:
            expected_rustc_identity = parse_rustc_identity(
                str(rustc_identity.get("output", ""))
            )
        except RuntimeError as error:
            raise ReleaseBundleError(
                "retained platform evidence has invalid rustc identity"
            ) from error
        if rustc_identity != expected_rustc_identity:
            raise ReleaseBundleError("retained platform evidence has invalid rustc identity")
        tck_payload = _json_from_snapshot(tck_snapshot, owner="retained TCK evidence")
        acceptance_payload = _json_from_snapshot(
            acceptance_snapshot,
            owner="retained acceptance evidence",
        )
        try:
            validate_release_evidence_payloads(
                tck_payload=tck_payload,
                acceptance_payload=acceptance_payload,
                expectations=expectations,
            )
        except RuntimeError as error:
            raise ReleaseBundleError(f"retained release evidence is invalid: {error}") from error
        if platform_payload.get("evidence") != {
            "tck": tck_payload.get("contentDigest"),
            "acceptance": acceptance_payload.get("contentDigest"),
        }:
            raise ReleaseBundleError(
                "retained platform evidence does not bind its TCK and acceptance reports"
            )
        tck_expectations = expectations.get("TCK")
        if not isinstance(tck_expectations, Mapping) or platform_payload.get(
            "contracts"
        ) != {
            "claimedProfiles": list(tck_expectations.get("claimed_profiles", ())),
            "conformanceProfileCatalogDigest": tck_expectations.get(
                "profile_catalog_digest"
            ),
            "schemaManifestDigest": tck_expectations.get("schema_manifest_digest"),
        }:
            raise ReleaseBundleError(
                "retained platform evidence does not bind stable conformance contracts"
            )
        raw_platform_artifacts = platform_payload.get("artifacts")
        if not isinstance(raw_platform_artifacts, list):
            raise ReleaseBundleError("retained platform evidence has no artifact set")
        observed_records: list[dict[str, object]] = []
        for raw_record in raw_platform_artifacts:
            if not isinstance(raw_record, dict):
                raise ReleaseBundleError("retained platform artifact record is invalid")
            filename = raw_record.get("filename")
            if not isinstance(filename, str) or filename not in artifact_by_filename:
                raise ReleaseBundleError("retained platform evidence names another artifact")
            record = artifact_by_filename[filename]
            distribution, version = _artifact_identity(filename)
            artifact_type = _artifact_type(filename)
            expected_record = {
                "filename": filename,
                "sha256": record["sha256"],
                "size": record["size"],
                "distribution": distribution,
                "version": version,
                "artifactType": artifact_type,
            }
            if raw_record != expected_record or not _artifact_matches_platform(
                filename,
                distribution=distribution,
                platform_identity=(os_name, python_version),
            ):
                raise ReleaseBundleError(
                    "retained platform evidence does not bind a compatible artifact"
                )
            prior_digest = covered_artifacts.get(filename)
            if prior_digest is not None and prior_digest != record["sha256"]:
                raise ReleaseBundleError("cross-platform artifact digest is inconsistent")
            covered_artifacts[filename] = str(record["sha256"])
            observed_records.append(expected_record)
        if observed_records != sorted(observed_records, key=lambda item: str(item["filename"])):
            raise ReleaseBundleError("retained platform artifacts are not canonical")
        if {
            (record["distribution"], record["artifactType"])
            for record in observed_records
        } != {
            (distribution, artifact_type)
            for distribution in ("graphblocks", "graphblocks-runtime", "graphblocks-testing")
            for artifact_type in ("wheel", "sdist")
        }:
            raise ReleaseBundleError(
                "retained platform evidence omits a first-party wheel or sdist"
            )
        build_environments.append(
            {
                "os": os_name,
                "python": python_version,
                "sourceDateEpoch": "315532800",
                "buildTools": expected_build_tools,
                "buildEnvironment": build_environment,
                "observedToolIdentities": {"rustc": expected_rustc_identity},
            }
        )
    if set(covered_artifacts) != set(artifact_by_filename):
        raise ReleaseBundleError("retained platform evidence does not cover every release artifact")
    _validate_release_artifact_names(tuple(artifact_by_filename))
    return build_environments, installed_release_distributions


def _verify_release_bundle(
    *,
    bundle_dir: Path,
    signature_bundle: Path | None = None,
    certificate_identity: str | None = None,
    certificate_oidc_issuer: str = SIGSTORE_ISSUER,
    cosign: str | Sequence[str] = "cosign",
    require_stable_signature: bool,
) -> dict[str, object]:
    manifest_path = bundle_dir / "release-manifest.json"
    manifest_snapshot = _snapshot_regular_file(manifest_path, owner="release manifest")
    manifest = _json_from_snapshot(manifest_snapshot, owner="release manifest")
    if manifest_snapshot.data != _canonical_json_bytes(manifest):
        raise ReleaseBundleError("release manifest does not use canonical JSON formatting")
    if manifest.get("formatVersion") != 1 or manifest.get("targetRelease") != "1.0":
        raise ReleaseBundleError("release manifest has an unsupported format or readiness")
    commit = manifest.get("gitCommit")
    if not isinstance(commit, str) or GIT_COMMIT.fullmatch(commit) is None:
        raise ReleaseBundleError("release manifest git commit is invalid")
    git_tree = manifest.get("gitTree")
    if not isinstance(git_tree, str) or GIT_COMMIT.fullmatch(git_tree) is None:
        raise ReleaseBundleError("release manifest git tree is invalid")
    release_ref = manifest.get("releaseRef")
    if not isinstance(release_ref, str):
        raise ReleaseBundleError("release manifest release ref is invalid")
    release_version = _release_version_from_ref(release_ref)
    if manifest.get("releaseVersion") != release_version:
        raise ReleaseBundleError("release manifest version does not match its release ref")
    is_final = release_ref == "refs/tags/v1.0.0"
    expected_manifest_keys = {
        "formatVersion",
        "targetRelease",
        "releaseRef",
        "releaseVersion",
        "readiness",
        "gitCommit",
        "gitTree",
        "distributionVersions",
        "platforms",
        "toolIdentities",
        "observedToolIdentities",
        "artifacts",
        "evidence",
        "metadata",
        "signaturePolicy",
        "externalGates",
        *(("promotionEvidence",) if is_final else ()),
    }
    if set(manifest) != expected_manifest_keys:
        raise ReleaseBundleError("release manifest has an unsupported shape")
    expected_readiness = (
        "promotion-authorized-signature-required" if is_final else "candidate"
    )
    if manifest.get("readiness") != expected_readiness:
        raise ReleaseBundleError("release manifest has an unsupported format or readiness")
    distribution_versions = manifest.get("distributionVersions")
    if not isinstance(distribution_versions, dict) or set(distribution_versions) != {
        "graphblocks",
        "graphblocks-runtime",
        "graphblocks-testing",
    }:
        raise ReleaseBundleError("release manifest distribution versions are invalid")
    if any(
        not isinstance(version, str) or not version
        for version in distribution_versions.values()
    ) or any(
        distribution_versions[distribution] != release_version
        for distribution in ("graphblocks", "graphblocks-testing")
    ):
        raise ReleaseBundleError("stable distribution versions do not match the release ref")
    expected_platforms = [
        {"os": os_name, "python": python_version}
        for os_name, python_version in SUPPORTED_PLATFORM_MATRIX
    ]
    if manifest.get("platforms") != expected_platforms:
        raise ReleaseBundleError("release manifest does not name the supported platform matrix")
    if manifest.get("toolIdentities") != dict(sorted(PINNED_RELEASE_TOOLS.items())):
        raise ReleaseBundleError("release manifest tool identities are not pinned")
    observed_tool_identities = manifest.get("observedToolIdentities")
    cosign_identity = (
        observed_tool_identities.get("cosign")
        if isinstance(observed_tool_identities, dict)
        else None
    )
    if not isinstance(cosign_identity, dict) or set(observed_tool_identities or {}) != {
        "cosign"
    }:
        raise ReleaseBundleError("release manifest has no observed Cosign identity")
    expected_cosign_identity = _parse_cosign_identity(
        str(cosign_identity.get("output", ""))
    )
    if cosign_identity != expected_cosign_identity:
        raise ReleaseBundleError("release manifest has an invalid observed Cosign identity")
    if manifest.get("signaturePolicy") != {
        "mechanism": "sigstore-keyless",
        "oidcIssuer": SIGSTORE_ISSUER,
        "repository": SIGSTORE_REPOSITORY,
        "workflow": SIGSTORE_WORKFLOW,
        "allowedRefPattern": RELEASE_REF_PATTERN,
        "subject": "release-manifest.json",
        "bundle": SIGNATURE_BUNDLE_NAME,
        "requiredForStable": True,
        "status": "signature-required" if is_final else "external-gate-pending",
    }:
        raise ReleaseBundleError("release manifest signature policy is invalid")
    expected_external_gates = (
        ["keyless-signing-identity"]
        if is_final
        else [
            "keyless-signing-identity",
            "release-index-credentials",
            "release-candidate-soak",
            "independent-api-review",
            "independent-security-review",
            "protected-final-ref",
            "authorized-real-staged-rehearsal",
        ]
    )
    if manifest.get("externalGates") != expected_external_gates:
        raise ReleaseBundleError("release manifest external gates are invalid")

    artifacts = _records_by_path(manifest.get("artifacts"), owner="artifacts")
    evidence = _records_by_path(manifest.get("evidence"), owner="evidence")
    metadata = _records_by_path(manifest.get("metadata"), owner="metadata")
    if set(artifacts) & set(evidence) or set(artifacts) & set(metadata) or set(evidence) & set(metadata):
        raise ReleaseBundleError("release manifest record categories overlap")
    if any(
        not path.startswith("artifacts/")
        or not (path.endswith(".whl") or path.endswith(".tar.gz"))
        for path in artifacts
    ):
        raise ReleaseBundleError("release manifest artifacts must be wheel or PEP 625 sdist files")
    _validate_release_artifact_names(tuple(Path(path).name for path in artifacts))
    artifact_versions: dict[str, str] = {}
    for path in artifacts:
        distribution, version = _artifact_identity(Path(path).name)
        prior_version = artifact_versions.setdefault(distribution, version)
        if prior_version != version:
            raise ReleaseBundleError("release artifacts contain conflicting versions")
    if artifact_versions != distribution_versions:
        raise ReleaseBundleError("release artifacts do not match manifest distribution versions")

    expected_files = {
        "release-manifest.json",
        *artifacts,
        *evidence,
        *metadata,
    }
    if signature_bundle is not None:
        expected_signature = bundle_dir / SIGNATURE_BUNDLE_NAME
        if os.path.abspath(signature_bundle) != os.path.abspath(expected_signature):
            raise ReleaseBundleError("Sigstore signature bundle must be inside the release closure")
        expected_files.add(SIGNATURE_BUNDLE_NAME)
    snapshots, observed_directories = _bundle_snapshots(
        bundle_dir,
        manifest_snapshot=manifest_snapshot,
    )
    if set(snapshots) != expected_files:
        raise ReleaseBundleError("release bundle contains missing or unexpected files")
    expected_directories = {
        Path(path).parent.as_posix()
        for path in expected_files
        if Path(path).parent.as_posix() != "."
    }
    expected_directories.add("evidence")
    if observed_directories != expected_directories:
        raise ReleaseBundleError("release bundle contains missing or unexpected directories")
    _verify_records(snapshots=snapshots, records=artifacts, owner="artifact")
    _verify_records(snapshots=snapshots, records=evidence, owner="evidence")
    _verify_records(snapshots=snapshots, records=metadata, owner="metadata")

    expected_evidence = {
        f"evidence/{os_name}-py{python_version.replace('.', '')}/{name}"
        for os_name, python_version in SUPPORTED_PLATFORM_MATRIX
        for name in EXPECTED_EVIDENCE
    }
    if set(evidence) != expected_evidence:
        raise ReleaseBundleError("release manifest evidence set is incomplete")
    evidence_content_digests = {
        relative_path: _require_content_digest(
            _json_from_snapshot(
                snapshots[relative_path],
                owner=f"release evidence {relative_path!r}",
            ),
            owner=f"release evidence {relative_path!r}",
        )
        for relative_path in evidence
    }
    required_metadata = {
        "SHA256SUMS",
        "SBOM.cdx.json",
        EXPECTATIONS_NAME,
        "provenance.intoto.json",
        "rehearsal.json",
        *((PROMOTION_EVIDENCE_NAME,) if is_final else ()),
    }
    if not required_metadata.issubset(metadata):
        raise ReleaseBundleError("release manifest metadata set is incomplete")
    checksum = metadata.get("SHA256SUMS")
    sbom_record = metadata.get("SBOM.cdx.json")
    provenance_record = metadata.get("provenance.intoto.json")
    expectations_record = metadata.get(EXPECTATIONS_NAME)
    rehearsal_record = metadata.get("rehearsal.json")
    if any(
        item is None
        for item in (
            checksum,
            sbom_record,
            provenance_record,
            expectations_record,
            rehearsal_record,
        )
    ):
        raise ReleaseBundleError("release manifest metadata set is incomplete")
    promotion_binding: Mapping[str, object] | None = None
    if is_final:
        raw_promotion_binding = _require_exact_keys(
            manifest.get("promotionEvidence"),
            {"path", "sha256", "size", "contentDigest"},
            owner="release manifest stable promotion evidence binding",
        )
        promotion_record = metadata.get(PROMOTION_EVIDENCE_NAME)
        if promotion_record is None or raw_promotion_binding != {
            **promotion_record,
            "contentDigest": raw_promotion_binding.get("contentDigest"),
        }:
            raise ReleaseBundleError(
                "release manifest stable promotion evidence record is invalid"
            )
        promotion_payload = _json_from_snapshot(
            snapshots[PROMOTION_EVIDENCE_NAME],
            owner="retained stable promotion evidence",
        )
        promotion_snapshot = snapshots[PROMOTION_EVIDENCE_NAME]
        (
            _validated_promotion_payload,
            promotion_content_digest,
            promotion_report_snapshots,
        ) = (
            _validate_promotion_evidence(
                promotion_snapshot,
                git_commit=commit,
                git_tree=git_tree,
                release_ref=release_ref,
                release_version=release_version,
                verify_source_diff=True,
                cosign=cosign,
            )
        )
        if promotion_payload != _validated_promotion_payload or raw_promotion_binding.get(
            "contentDigest"
        ) != promotion_content_digest:
            raise ReleaseBundleError(
                "release manifest stable promotion evidence digest is invalid"
            )
        promotion_binding = raw_promotion_binding
        required_metadata.update(
            snapshot.path.relative_to(bundle_dir).as_posix()
            for snapshot in promotion_report_snapshots
        )
    elif "promotionEvidence" in manifest:
        raise ReleaseBundleError(
            "release candidate manifest must not bind stable promotion evidence"
        )
    if set(metadata) != required_metadata:
        raise ReleaseBundleError("release manifest metadata set is incomplete or unexpected")
    expectations_payload = _json_from_snapshot(
        snapshots[EXPECTATIONS_NAME],
        owner="release expectations",
    )
    frozen_expectations, expectations_content_digest = _frozen_release_expectations(
        expectations_payload,
        git_commit=commit,
        git_tree=git_tree,
        release_ref=release_ref,
        release_version=release_version,
    )
    build_environments, installed_distributions = _verify_platform_evidence(
        snapshots=snapshots,
        artifacts=artifacts,
        expectations=frozen_expectations,
    )
    expected_checksums = _artifact_checksum_manifest(tuple(artifacts.values()))
    try:
        observed_checksums = snapshots["SHA256SUMS"].data.decode("ascii")
    except UnicodeError as error:
        raise ReleaseBundleError("SHA256SUMS is not ASCII") from error
    if observed_checksums != expected_checksums:
        raise ReleaseBundleError("SHA256SUMS is not the canonical artifact checksum manifest")

    artifact_records_by_filename = {
        Path(path).name: record for path, record in artifacts.items()
    }
    sbom_payload = _json_from_snapshot(snapshots["SBOM.cdx.json"], owner="release SBOM")
    _validate_sbom(
        sbom_payload,
        artifact_records_by_filename,
        installed_distributions=installed_distributions,
    )
    _verify_provenance(
        _json_from_snapshot(
            snapshots["provenance.intoto.json"],
            owner="release provenance",
        ),
        manifest=manifest,
        artifacts=artifacts,
        evidence=evidence,
        evidence_content_digests=evidence_content_digests,
        expectations=expectations_record or {},
        expectations_content_digest=expectations_content_digest,
        promotion_evidence=promotion_binding,
        sbom=sbom_record or {},
        build_environments=build_environments,
    )
    _verify_rehearsal(
        _json_from_snapshot(snapshots["rehearsal.json"], owner="release rehearsal"),
        manifest=manifest,
        artifacts=artifacts,
    )

    if signature_bundle is not None:
        if certificate_identity is None:
            raise ReleaseBundleError("signature verification requires a certificate identity")
        _verify_sigstore_signature(
            manifest_snapshot=manifest_snapshot,
            signature_snapshot=snapshots[SIGNATURE_BUNDLE_NAME],
            certificate_identity=certificate_identity,
            certificate_oidc_issuer=certificate_oidc_issuer,
            expected_release_ref=release_ref,
            cosign=cosign,
            expected_cosign_identity=expected_cosign_identity,
        )
    elif certificate_identity is not None:
        raise ReleaseBundleError("certificate identity was provided without a signature bundle")
    elif is_final and require_stable_signature:
        raise ReleaseBundleError(
            "final stable release verification requires its Sigstore signature bundle"
        )
    return manifest


def verify_release_bundle(
    *,
    bundle_dir: Path,
    signature_bundle: Path | None = None,
    certificate_identity: str | None = None,
    certificate_oidc_issuer: str = SIGSTORE_ISSUER,
    cosign: str | Sequence[str] = "cosign",
) -> dict[str, object]:
    return _verify_release_bundle(
        bundle_dir=bundle_dir,
        signature_bundle=signature_bundle,
        certificate_identity=certificate_identity,
        certificate_oidc_issuer=certificate_oidc_issuer,
        cosign=cosign,
        require_stable_signature=True,
    )


def _resolve_git_commit(ref: str) -> str:
    commit = _git_output("rev-parse", "--verify", f"{ref}^{{commit}}")
    if GIT_COMMIT.fullmatch(commit) is None:
        raise ReleaseBundleError("Git ref resolved to an invalid commit identity")
    return commit


def _current_git_commit() -> str:
    return _resolve_git_commit("HEAD")


def _current_git_tree() -> str:
    return _git_output("rev-parse", "HEAD^{tree}")


def _git_output(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            check=True,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseBundleError("release source Git identity could not be observed") from error
    return completed.stdout.strip()


def _assert_clean_source_checkout() -> None:
    status = _git_output("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ReleaseBundleError("release source checkout is not clean")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble and verify an immutable GraphBlocks release supply-chain bundle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--platform-inputs-dir", type=Path, required=True)
    assemble.add_argument("--output-dir", type=Path, required=True)
    assemble.add_argument("--git-commit", required=True)
    assemble.add_argument("--release-ref", required=True)
    assemble.add_argument("--builder-id", required=True)
    assemble.add_argument("--invocation-id", required=True)
    assemble.add_argument("--promotion-evidence", type=Path)
    assemble.add_argument("--cosign", default="cosign")
    freeze_matrix_report = subparsers.add_parser("freeze-candidate-matrix-report")
    freeze_matrix_report.add_argument("--output-dir", type=Path, required=True)
    freeze_matrix_report.add_argument("--candidate-ref", required=True)
    freeze_matrix_report.add_argument("--candidate-commit", required=True)
    freeze_matrix_report.add_argument("--run-id", required=True)
    freeze_report = subparsers.add_parser("freeze-promotion-report")
    freeze_report.add_argument("--input", type=Path, required=True)
    freeze_report.add_argument("--output-dir", type=Path, required=True)
    freeze_report.add_argument(
        "--report-type", choices=PROMOTION_REPORT_TYPES, required=True
    )
    freeze_report.add_argument("--candidate-ref", required=True)
    freeze_report.add_argument("--candidate-commit", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle-dir", type=Path, required=True)
    verify.add_argument("--signature-bundle", type=Path)
    verify.add_argument("--certificate-identity")
    verify.add_argument("--certificate-oidc-issuer", default=SIGSTORE_ISSUER)
    verify.add_argument("--cosign", default="cosign")
    args = parser.parse_args(argv)

    if args.command == "assemble":
        assemble_release_bundle(
            platform_inputs_dir=args.platform_inputs_dir.resolve(),
            output_dir=args.output_dir.resolve(),
            git_commit=args.git_commit,
            release_ref=args.release_ref,
            builder_id=args.builder_id,
            invocation_id=args.invocation_id,
            promotion_evidence=(
                args.promotion_evidence.resolve()
                if args.promotion_evidence is not None
                else None
            ),
            cosign=args.cosign,
        )
        print(f"assembled release bundle in {args.output_dir}")
    elif args.command == "freeze-candidate-matrix-report":
        frozen_report = freeze_candidate_matrix_report(
            output_dir=args.output_dir.resolve(),
            candidate_ref=args.candidate_ref,
            candidate_commit=args.candidate_commit,
            run_id=args.run_id,
        )
        print(
            "froze candidate matrix report "
            f"sha256:{frozen_report.sha256} in {frozen_report.path}"
        )
    elif args.command == "freeze-promotion-report":
        frozen_report = freeze_promotion_report(
            input_path=args.input.resolve(),
            output_dir=args.output_dir.resolve(),
            report_type=args.report_type,
            candidate_ref=args.candidate_ref,
            candidate_commit=args.candidate_commit,
        )
        print(
            "froze promotion report "
            f"sha256:{frozen_report.sha256} in {frozen_report.path}"
        )
    else:
        verify_release_bundle(
            bundle_dir=args.bundle_dir.resolve(),
            signature_bundle=(
                args.signature_bundle.absolute() if args.signature_bundle is not None else None
            ),
            certificate_identity=args.certificate_identity,
            certificate_oidc_issuer=args.certificate_oidc_issuer,
            cosign=args.cosign,
        )
        print(f"verified release bundle in {args.bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
