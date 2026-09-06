"""Call-time seams for helpers that tests monkeypatch on ``graphblocks.server``.

Tests replace attributes on the ``graphblocks.server`` module — for example
``compile_graph`` to stub run compilation, or ``_server_request_json_body``
to observe (or delay) request parsing — and expect every server code path
to resolve the patched value. The handler families now live in private
``_server_*`` modules, which therefore import these names from here instead
of binding the implementations directly: each delegator below resolves the
seam installed by ``graphblocks.server`` (a closure over its own module
attribute) on every call, falling back to the real implementation when the
facade has not been imported.

``graphblocks.server`` installs the seams downward at import time; nothing
here imports it, because the internal syntactic import graph must stay
acyclic and package-root imports are rejected even inside function bodies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast, TYPE_CHECKING

from ._server_validation import (
    _canonical_json_size_bytes as _direct_canonical_json_size_bytes,
    _server_request_json_body as _direct_server_request_json_body,
)
from .compiler import compile_graph as _direct_compile_graph

if TYPE_CHECKING:
    from ._server_transport import ServerRequest

_SEAMS: dict[str, Callable[..., object]] = {}


def install_server_seam(name: str, seam: Callable[..., object]) -> None:
    """Install one named seam (done by ``graphblocks.server`` at import)."""
    _SEAMS[name] = seam


def compile_graph(*args: object, **kwargs: object) -> object:
    seam = _SEAMS.get("compile_graph", _direct_compile_graph)
    return seam(*args, **kwargs)


def _server_request_json_body(request: ServerRequest, owner: str) -> object:
    seam = _SEAMS.get("_server_request_json_body", _direct_server_request_json_body)
    return seam(request, owner)


def _canonical_json_size_bytes(value: object) -> int:
    seam = _SEAMS.get("_canonical_json_size_bytes")
    if seam is None:
        return _direct_canonical_json_size_bytes(value)
    return cast(int, seam(value))
