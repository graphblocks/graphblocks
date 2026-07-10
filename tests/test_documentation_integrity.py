from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def test_project_markdown_links_resolve() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "GOVERNANCE.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]
    failures: list[str] = []

    for document in documents:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)} -> {raw_target}")

    assert not failures, "unresolved Markdown links:\n" + "\n".join(failures)


def test_living_documentation_has_one_authority_tree() -> None:
    legacy_root = ROOT / "docs" / "upstream" / "GraphBlocks_v1.0_Final"
    remaining = {
        path.relative_to(legacy_root).as_posix()
        for path in legacy_root.rglob("*")
        if path.is_file()
    }

    assert remaining == {
        "examples/README.md",
        "examples/01-enterprise-federated-rag.yaml",
        "examples/02-document-ingestion.yaml",
        "examples/03-policy-governed-chat.yaml",
        "examples/04-tui-workspace-assistant.yaml",
        "examples/05-authority-backed-advisory.yaml",
        "examples/06-bounded-research-orchestrator.yaml",
        "examples/07-verified-rtl-workspace-trial.yaml",
        "examples/08-kubernetes-production-deployment.yaml",
        "examples/09-observability-profile.yaml",
        "examples/10-realtime-voice-extension.yaml",
        "examples/11-coding-agent-background-callbacks.yaml",
    }
    assert (ROOT / "docs" / "specification" / "README.md").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "package-catalog.yaml").is_file()
    assert (ROOT / "src" / "graphblocks" / "data" / "conformance-profiles.yaml").is_file()
    assert (ROOT / "profiles" / "policy-profiles.yaml").is_file()
