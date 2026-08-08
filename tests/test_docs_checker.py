from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest
import yaml

from tools import check_docs
from tools.check_docs import check_markdown_documents, discover_markdown_documents


ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "tools" / "check_docs.py"
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


def test_document_reader_accepts_windows_path_descriptor_ctime_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "README.md"
    document.write_bytes(b"# Verified documentation\n")
    descriptor_status = document.lstat()
    path_status = SimpleNamespace(
        st_dev=descriptor_status.st_dev,
        st_ino=descriptor_status.st_ino,
        st_mode=descriptor_status.st_mode,
        st_size=descriptor_status.st_size,
        st_mtime_ns=descriptor_status.st_mtime_ns,
        st_ctime_ns=descriptor_status.st_ctime_ns + 1,
    )
    original_lstat = Path.lstat

    def platform_lstat(path: Path):
        if path == document:
            return path_status
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", platform_lstat)

    blob = check_docs._read_regular_file(
        document,
        owner="Markdown document",
        max_bytes=1_024,
    )

    assert blob.data == b"# Verified documentation\n"


def test_document_reader_rejects_descriptor_change_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "README.md"
    document.write_bytes(b"# Verified documentation\n")
    original_fstat = check_docs.os.fstat
    fstat_calls = 0

    def changing_fstat(descriptor: int):
        nonlocal fstat_calls
        descriptor_status = original_fstat(descriptor)
        fstat_calls += 1
        if fstat_calls == 1:
            return descriptor_status
        return SimpleNamespace(
            st_dev=descriptor_status.st_dev,
            st_ino=descriptor_status.st_ino,
            st_mode=descriptor_status.st_mode,
            st_size=descriptor_status.st_size,
            st_mtime_ns=descriptor_status.st_mtime_ns,
            st_ctime_ns=descriptor_status.st_ctime_ns + 1,
        )

    monkeypatch.setattr(check_docs.os, "fstat", changing_fstat)

    with pytest.raises(
        check_docs.DocsCheckError,
        match="changed while it was read",
    ):
        check_docs._read_regular_file(
            document,
            owner="Markdown document",
            max_bytes=1_024,
        )


@pytest.mark.parametrize(
    ("relative_path", "claim"),
    (
        (
            "docs/project/status.md",
            "The checkout passes more than 2,700 Python tests.",
        ),
        (
            "docs/project/remaining-work.md",
            "The complete suite passes with 2,625 tests.",
        ),
    ),
)
def test_markdown_checker_rejects_fixed_test_counts_in_status_documents(
    tmp_path: Path,
    relative_path: str,
    claim: str,
) -> None:
    document = tmp_path / relative_path
    document.parent.mkdir(parents=True)
    document.write_text(f"# Status\n\n{claim}\n", encoding="utf-8")

    failures = check_markdown_documents(tmp_path, (document,))

    assert failures == [
        f"{relative_path}:3: fixed test counts must remain in commit-bound CI evidence"
    ]


def test_markdown_checker_accepts_commonmark_links_and_github_heading_anchors(
    tmp_path: Path,
) -> None:
    index = tmp_path / "README.md"
    guide = tmp_path / "docs" / "guide.md"
    guide.parent.mkdir()
    index.write_text(
        """# Index

[inline](docs/guide.md#repeat-1)
[collision](docs/guide.md#repeat-1-1)
[emoji](docs/guide.md#-emoji)
[emphasis](docs/guide.md#emphasis)
[reference][guide]
[same document](#index)

[guide]: <docs/guide.md#explicit-anchor> "Guide"

`[inline code](missing-inline.md)`

~~~markdown
[fenced code](missing-fenced.md)
~~~
""",
        encoding="utf-8",
    )
    guide.write_text(
        """# Guide

## Repeat
## Repeat
## Repeat-1
## 😄 emoji
## _emphasis_

<a id="explicit-anchor"></a>
""",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (index, guide)) == []


def test_commonmark_ast_ignores_non_links_and_reports_undefined_references(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    document.write_text(
        """# Parser boundary
[broken][missing]
[shortcut]
See [sentence shortcut] here.
![missing image]
<span data-note="[x][html-missing]">safe</span>
### [BUG-001] literal issue identifier
> [!CAUTION]
[explicit][BUG-001]
[multiline
shortcut]
](missing-plain-text.md)
\\[escaped](missing-escaped.md)

`
[code shortcut]
`

> ~~~markdown
> [fenced](missing-fenced.md)
> ~~~
""",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:10: unresolved Markdown reference [multiline shortcut]",
        "README.md:2: unresolved Markdown reference [missing]",
        "README.md:3: unresolved Markdown reference [shortcut]",
        "README.md:4: unresolved Markdown reference [sentence shortcut]",
        "README.md:5: unresolved Markdown reference [missing image]",
        "README.md:9: unresolved Markdown reference [bug-001]",
    ]


def test_shortcut_literal_exceptions_are_case_context_and_image_sensitive(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    document.write_text(
        """### [BUG-001] issue heading
Cross-reference [BUG-001].
> [!CAUTION]
Paragraph [!CAUTION].
[bug-001]
[Bug-001]
![BUG-001]
![!CAUTION]
""",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:4: unresolved Markdown reference [!caution]",
        "README.md:5: unresolved Markdown reference [bug-001]",
        "README.md:6: unresolved Markdown reference [bug-001]",
        "README.md:7: unresolved Markdown reference [bug-001]",
        "README.md:8: unresolved Markdown reference [!caution]",
    ]


def test_docs_checker_fails_closed_when_registered_projection_fails(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    registry = tmp_path / "docs" / "project" / "generated-documentation.yaml"
    registry.parent.mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "# Fixture\n\n<!-- Generated by tools/fail_projection.py. -->\n",
        encoding="utf-8",
    )
    (tmp_path / "source.txt").write_text("authority\n", encoding="utf-8")
    (tools / "fail_projection.py").write_text(
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    registry.write_text(
        """formatVersion: 1
projections:
  - id: failing-projection
    generator: tools/fail_projection.py
    arguments: [--check]
    sources: [source.txt]
    outputs: [README.md]
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert (
        "generated documentation is stale: tools/fail_projection.py --check"
    ) in completed.stderr


def test_generated_projection_digest_binds_declared_file_content(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    registry = tmp_path / "docs" / "project" / "generated-documentation.yaml"
    registry.parent.mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        """# Fixture

<!-- Generated by tools/check_projection.py. -->

~~~markdown
<!-- Generated by tools/not_a_projection.py. -->
~~~
""",
        encoding="utf-8",
    )
    source = tmp_path / "source.txt"
    source.write_text("first authority\n", encoding="utf-8")
    (tools / "check_projection.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    registry.write_text(
        """formatVersion: 1
projections:
  - id: passing-projection
    generator: tools/check_projection.py
    arguments: [--check]
    sources: [source.txt]
    outputs: [README.md]
""",
        encoding="utf-8",
    )
    first_report = tmp_path / "first-report.json"
    first = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(tmp_path),
            "--report",
            str(first_report),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr

    source.write_text("second authority\n", encoding="utf-8")
    second_report = tmp_path / "second-report.json"
    second = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(tmp_path),
            "--report",
            str(second_report),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr

    first_payload = json.loads(first_report.read_text(encoding="utf-8"))
    second_payload = json.loads(second_report.read_text(encoding="utf-8"))
    assert first_payload["documentSetDigest"] == second_payload["documentSetDigest"]
    assert (
        first_payload["generatedProjectionDigest"]
        != second_payload["generatedProjectionDigest"]
    )
    assert first_payload["resultDigest"] != second_payload["resultDigest"]


def test_generated_registry_rejects_an_undeclared_marker_output(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    registry = tmp_path / "docs" / "project" / "generated-documentation.yaml"
    registry.parent.mkdir(parents=True)
    marker = "<!-- Generated by tools/check_projection.py. -->\n"
    (tmp_path / "README.md").write_text(marker, encoding="utf-8")
    (tmp_path / "EXTRA.md").write_text(marker, encoding="utf-8")
    (tmp_path / "source.txt").write_text("authority\n", encoding="utf-8")
    (tools / "check_projection.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    registry.write_text(
        """formatVersion: 1
projections:
  - id: passing-projection
    generator: tools/check_projection.py
    arguments: [--check]
    sources: [source.txt]
    outputs: [README.md]
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "generated-documentation output ownership differs" in completed.stderr


def test_generated_registry_rejects_an_ancestor_symlink_escape(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    registry = tmp_path / "docs" / "project" / "generated-documentation.yaml"
    registry.parent.mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "<!-- Generated by tools/check_projection.py. -->\n",
        encoding="utf-8",
    )
    (tools / "check_projection.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "source.txt").write_text("authority\n", encoding="utf-8")
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    registry.write_text(
        """formatVersion: 1
projections:
  - id: passing-projection
    generator: tools/check_projection.py
    arguments: [--check]
    sources: [linked/source.txt]
    outputs: [README.md]
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "escapes the repository: linked/source.txt" in completed.stderr


def test_markdown_checker_reports_missing_paths_fragments_and_case(
    tmp_path: Path,
) -> None:
    index = tmp_path / "README.md"
    guide = tmp_path / "Guide.md"
    index.write_text(
        """# Index

[missing](missing.md)
[fragment](Guide.md#missing-heading)
[case](guide.md)
""",
        encoding="utf-8",
    )
    guide.write_text("# Existing heading\n", encoding="utf-8")

    failures = check_markdown_documents(tmp_path, (index, guide))

    assert failures == [
        "README.md:3: local link target does not exist: missing.md",
        "README.md:4: Markdown fragment does not exist in Guide.md: #missing-heading",
        "README.md:5: local link target does not exist: guide.md",
    ]


def test_markdown_checker_rejects_links_outside_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    document = repository / "README.md"
    document.write_text("[outside](../outside.md)\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")

    assert check_markdown_documents(repository, (document,)) == [
        "README.md:1: local link escapes the repository: ../outside.md"
    ]


def test_markdown_checker_rejects_root_relative_web_paths(tmp_path: Path) -> None:
    document = tmp_path / "README.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    document.write_text("[not repository-relative](/docs/guide.md)\n", encoding="utf-8")

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:1: unsupported local link: /docs/guide.md"
    ]


def test_markdown_checker_validates_non_markdown_source_line_fragments(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    (tmp_path / "data.json").write_text("{\n}\n", encoding="utf-8")
    document.write_text(
        "[valid](data.json#L1-L2)\n[missing](data.json#L3)\n",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:2: source line fragment does not exist in data.json: #L3"
    ]


def test_markdown_checker_reports_duplicate_explicit_anchors(tmp_path: Path) -> None:
    document = tmp_path / "README.md"
    document.write_text(
        """# Title

<a id="duplicate"></a>
<span id='duplicate'></span>
""",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:4: duplicate Markdown anchor #duplicate (first declared on line 3)"
    ]


def test_markdown_checker_orders_explicit_and_generated_anchors_by_source_line(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    document.write_text('<a id="title"></a>\n# Title\n', encoding="utf-8")

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:2: duplicate Markdown anchor #title (first declared on line 1)"
    ]


def test_html_comments_and_prefixed_attributes_are_not_links_or_anchors(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    document.write_text(
        """<!-- <a id=commented href=commented-missing.md></a> -->
<a id=real data-href=prefixed-missing.md></a>
<meta name=metadata>
[real](#real)
[commented](#commented)
[metadata](#metadata)
<a href=actual-missing.md>actual</a>
""",
        encoding="utf-8",
    )

    assert check_markdown_documents(tmp_path, (document,)) == [
        "README.md:5: Markdown fragment does not exist in README.md: #commented",
        "README.md:6: Markdown fragment does not exist in README.md: #metadata",
        "README.md:7: local link target does not exist: actual-missing.md",
    ]


def test_repository_document_discovery_covers_living_markdown() -> None:
    documents = {
        path.relative_to(ROOT).as_posix() for path in discover_markdown_documents(ROOT)
    }

    assert {
        "README.md",
        "README.ko.md",
        "README.zh-CN.md",
        "docs/project/status.md",
        "docs/specification/README.md",
        "examples/README.md",
        "compatibility/README.md",
        "crates/graphblocks/README.md",
        "packages/graphblocks-npm/README.md",
    } <= documents
    assert all("/.venv/" not in f"/{path}/" for path in documents)
    assert all("/target/" not in f"/{path}/" for path in documents)


def test_repository_docs_generated_facts_and_content_digests_are_current(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(ROOT),
            "--report",
            str(tmp_path / "docs-check.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "docs-check.json").read_text(encoding="utf-8"))
    result_digest = report.pop("resultDigest")
    expected_result_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    local_links_checked = report.pop("localLinksChecked")
    external_links_skipped = report.pop("externalLinksSkipped")
    checker_digest = report.pop("checkerDigest")
    document_set_digest = report.pop("documentSetDigest")
    projection_digest = report.pop("generatedProjectionDigest")
    assert report == {
        "formatVersion": 1,
        "status": "passed",
        "documentsChecked": len(discover_markdown_documents(ROOT)),
        "generatedChecks": [
            "tools/generate_status_readiness.py --check",
            "tools/generate_stdlib_inventory.py --check",
        ],
    }
    assert result_digest == expected_result_digest
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", checker_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", document_set_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", projection_digest)
    assert isinstance(local_links_checked, int) and local_links_checked > 100
    assert isinstance(external_links_skipped, int) and external_links_skipped > 10


def test_docs_workflow_is_always_run_fast_and_executes_the_closed_checker() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(workflow) == {
        "name",
        "on",
        "permissions",
        "concurrency",
        "jobs",
    }
    assert workflow["on"] == {"push": {}, "pull_request": {}}
    assert workflow["permissions"] == {"contents": "read"}

    assert set(workflow["jobs"]) == {"docs"}
    job = workflow["jobs"]["docs"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "5"
    assert "strategy" not in job
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses == [
        "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    ]
    runs = "\n".join(step["run"] for step in job["steps"] if "run" in step)
    assert 'python -m pip install -e ".[test]"' in runs
    assert "python tools/check_docs.py" in runs
    assert "cargo " not in runs
    assert "pytest" not in runs


def test_documentation_integrity_is_a_named_release_gate() -> None:
    matrix = yaml.safe_load(
        (ROOT / "docs" / "project" / "stable-release-matrix.yaml").read_text(
            encoding="utf-8"
        )
    )
    gates = {gate["id"]: gate for gate in matrix["releaseGates"]}
    gate = gates["REL-DOCS-INTEGRITY"]

    assert "REL-DOCS-INTEGRITY" in matrix["globalRequiredGates"]
    assert gate == {
        "id": "REL-DOCS-INTEGRITY",
        "description": (
            "Every living Markdown path and fragment resolves locally and every "
            "declared generated documentation projection is current."
        ),
        "readiness": "candidate-enforced",
        "blocksTargetRelease": True,
        "externalNetworkAvailabilityClaimed": False,
        "fixedTestCountPolicy": {
            "documents": [
                "docs/project/status.md",
                "docs/project/remaining-work.md",
            ],
            "numericClaimsAllowed": False,
            "authority": "commit-bound-ci-junit",
            "checker": "tools/check_docs.py",
            "regression": "tests/test_docs_checker.py",
            "observedCiEvidence": {
                "runId": 31246430572,
                "jobId": 93075701399,
                "headSha": "5cf1f785b4b5118c60a663620161946c56691847",
                "conclusion": "success",
                "durationSeconds": 25,
            },
        },
        "v1WireRoadmapPolicy": {
            "documents": [
                "docs/project/roadmap.md",
                "docs/project/remaining-work.md",
            ],
            "wireVersions": [
                "graphblocks.ai/v1:Graph",
                "graphblocks.ai/v1:PluginManifest",
            ],
            "schemas": [
                "schemas/graphblocks.ai/v1/graph.schema.json",
                "schemas/graphblocks.ai/v1/plugin-manifest.schema.json",
            ],
            "requiredGates": ["REL-WIRE-V1", "REL-CLOSED-SCHEMA"],
            "compatibilityClaimRequiresAllApplicableGates": True,
            "regression": "tests/test_documentation_integrity.py",
            "observedCiEvidence": {
                "runId": 31246722408,
                "jobId": 93076456592,
                "headSha": "9a3ba62e459b9d1de058b8caab507880b93fbbe4",
                "conclusion": "success",
            },
        },
        "evidence": [
            "docs/project/generated-documentation.yaml",
            "tools/check_docs.py",
            "tools/generate_status_readiness.py",
            "tools/generate_stdlib_inventory.py",
            "tests/test_docs_checker.py",
            "tests/test_documentation_integrity.py",
            "docs/project/roadmap.md",
            "docs/project/remaining-work.md",
            "schemas/graphblocks.ai/v1/graph.schema.json",
            "schemas/graphblocks.ai/v1/plugin-manifest.schema.json",
            "pyproject.toml",
            ".github/workflows/docs.yml",
        ],
    }

    remediation_map = yaml.safe_load(
        (ROOT / "docs" / "project" / "audit-remediation-map.yaml").read_text(
            encoding="utf-8"
        )
    )
    quality = next(
        workstream
        for workstream in remediation_map["workstreams"]
        if workstream["id"] == "QG-QUALITY-AND-CI"
    )
    assert "GB-QA-011" in quality["findings"]
    assert "REL-DOCS-INTEGRITY" in quality["releaseGates"]

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert {
        "github-slugger==0.0.3",
        "markdown-it-py>=4.2,<5",
    } <= set(project["project"]["optional-dependencies"]["test"])
