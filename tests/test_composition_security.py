from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import graphblocks.composition as composition_module
from graphblocks.composition import CompositionError, compose_documents


MESSAGE_SCHEMA = "graphblocks.ai/Message@1"
PROMPT_SCHEMA = "graphblocks.ai/Prompt@1"
COMPOSITION_API_VERSION = "graphblocks.ai/composition/v1alpha1"


def _fragment() -> dict[str, object]:
    return {
        "apiVersion": COMPOSITION_API_VERSION,
        "kind": "GraphFragment",
        "metadata": {"name": "render-prompt"},
        "spec": {
            "interface": {
                "inputs": {"message": MESSAGE_SCHEMA},
                "outputs": {"prompt": PROMPT_SCHEMA},
            },
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Composed {message.text}"},
                }
            },
            "edges": [
                {"from": "$input.message", "to": "render.message"},
                {"from": "render.prompt", "to": "$output.prompt"},
            ],
        },
    }


def _graph(*, composed: bool) -> dict[str, object]:
    graph: dict[str, object] = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "secure-composition"},
        "spec": {
            "interface": {
                "inputs": {"message": MESSAGE_SCHEMA},
                "outputs": {"prompt": PROMPT_SCHEMA},
            },
            "nodes": {},
        },
    }
    if composed:
        graph["spec"] = {
            "interface": {
                "inputs": {"message": MESSAGE_SCHEMA},
                "outputs": {"prompt": PROMPT_SCHEMA},
            },
            "composition": {
                "apiVersion": COMPOSITION_API_VERSION,
                "imports": {"prompt": {"path": "fragment.yaml"}},
                "slots": {
                    "answer-slot": {
                        "interface": {
                            "inputs": {"message": MESSAGE_SCHEMA},
                            "outputs": {"prompt": PROMPT_SCHEMA},
                        },
                        "fill": {"fragment": "prompt/render-prompt"},
                    }
                },
            },
            "nodes": {
                "answer": {
                    "slot": "answer-slot",
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            },
        }
    return graph


def _write_yaml(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


@pytest.mark.parametrize("source_kind", ["entry", "import"])
@pytest.mark.parametrize("descriptor_relative", [True, False])
def test_composition_rejects_source_swapped_to_outside_symlink_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    descriptor_relative: bool,
) -> None:
    graph_path = tmp_path / "graph.yaml"
    fragment_path = tmp_path / "fragment.yaml"
    outside_path = tmp_path.parent / f"{tmp_path.name}-{source_kind}-outside.yaml"
    if source_kind == "entry":
        _write_yaml(graph_path, _graph(composed=False))
        _write_yaml(outside_path, _graph(composed=False))
        swapped_path = graph_path.resolve()
    else:
        _write_yaml(graph_path, _graph(composed=True))
        _write_yaml(fragment_path, _fragment())
        _write_yaml(outside_path, _fragment())
        swapped_path = fragment_path.resolve()

    original_is_file = Path.is_file
    original_read_bytes = Path.read_bytes
    swapped = False
    outside_read = False

    def swap_after_validation(path: Path) -> bool:
        nonlocal swapped
        result = original_is_file(path)
        if path == swapped_path and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(outside_path)
        return result

    def track_outside_read(path: Path) -> bytes:
        nonlocal outside_read
        if path.resolve() == outside_path:
            outside_read = True
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "is_file", swap_after_validation)
    monkeypatch.setattr(Path, "read_bytes", track_outside_read)
    if not descriptor_relative:
        monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert swapped
    assert not outside_read
    assert captured.value.code == "CompositionSymlinkRejected"


@pytest.mark.parametrize("source_kind", ["entry", "import"])
@pytest.mark.parametrize("descriptor_relative", [True, False])
def test_composition_fails_closed_when_source_is_swapped_during_secure_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    descriptor_relative: bool,
) -> None:
    graph_path = tmp_path / "graph.yaml"
    fragment_path = tmp_path / "fragment.yaml"
    outside_path = tmp_path.parent / f"{tmp_path.name}-{source_kind}-open-race.yaml"
    if source_kind == "entry":
        _write_yaml(graph_path, _graph(composed=False))
        _write_yaml(outside_path, _graph(composed=False))
        swapped_path = graph_path.resolve()
    else:
        _write_yaml(graph_path, _graph(composed=True))
        _write_yaml(fragment_path, _fragment())
        _write_yaml(outside_path, _fragment())
        swapped_path = fragment_path.resolve()

    original_open = os.open
    original_fdopen = os.fdopen
    swapped = False
    outside_read = False

    def swap_during_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if descriptor_relative:
            should_swap = dir_fd is not None and os.fspath(path) == swapped_path.name
        else:
            should_swap = dir_fd is None and os.fspath(path) == os.fspath(swapped_path)
        if should_swap and not swapped:
            swapped = True
            swapped_path.unlink()
            swapped_path.symlink_to(outside_path)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def track_outside_read(fd, *args, **kwargs):
        nonlocal outside_read
        if os.path.samestat(os.fstat(fd), outside_path.stat()):
            outside_read = True
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(composition_module.os, "open", swap_during_open)
    monkeypatch.setattr(composition_module.os, "fdopen", track_outside_read)
    if descriptor_relative:
        monkeypatch.setattr(composition_module.os, "supports_dir_fd", {swap_during_open})
    else:
        monkeypatch.delattr(composition_module.os, "O_NOFOLLOW")

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert swapped
    assert not outside_read
    assert captured.value.code in {
        "CompositionImportOutsideRoot",
        "CompositionSymlinkRejected",
    }


@pytest.mark.parametrize("source_kind", ["entry", "import"])
@pytest.mark.parametrize("descriptor_relative", [True, False])
def test_composition_rejects_source_swapped_to_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    descriptor_relative: bool,
) -> None:
    make_fifo = getattr(os, "mkfifo", None)
    non_blocking = getattr(os, "O_NONBLOCK", 0)
    if make_fifo is None or not non_blocking:
        pytest.skip("FIFO non-blocking opens are unavailable")

    graph_path = tmp_path / "graph.yaml"
    fragment_path = tmp_path / "fragment.yaml"
    if source_kind == "entry":
        _write_yaml(graph_path, _graph(composed=False))
        swapped_path = graph_path.resolve()
    else:
        _write_yaml(graph_path, _graph(composed=True))
        _write_yaml(fragment_path, _fragment())
        swapped_path = fragment_path.resolve()

    original_open = os.open
    swapped = False

    def swap_to_fifo(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if descriptor_relative:
            should_swap = dir_fd is not None and os.fspath(path) == swapped_path.name
        else:
            should_swap = dir_fd is None and os.fspath(path) == os.fspath(swapped_path)
        if should_swap and not swapped:
            swapped = True
            swapped_path.unlink()
            make_fifo(swapped_path)
            assert flags & non_blocking, "composition source opens must not block on FIFOs"
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(composition_module.os, "open", swap_to_fifo)
    if descriptor_relative:
        monkeypatch.setattr(composition_module.os, "supports_dir_fd", {swap_to_fifo})
    else:
        monkeypatch.delattr(composition_module.os, "O_NOFOLLOW")

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert swapped
    assert captured.value.code == "CompositionInvalidImport"
