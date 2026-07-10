from __future__ import annotations

from collections.abc import Mapping

from graphblocks.worker import WorkerInvokeRequest, WorkerInvokeResult


def invoke(request: WorkerInvokeRequest) -> WorkerInvokeResult:
    if request.block != "examples.python.normalize-text@1":
        raise ValueError(f"unsupported Python custom block {request.block!r}")
    if not isinstance(request.inputs, Mapping):
        raise TypeError("normalize-text inputs must be a mapping")
    text = request.inputs.get("text")
    if not isinstance(text, str):
        raise TypeError("normalize-text input text must be a string")
    if not isinstance(request.config, Mapping):
        raise TypeError("normalize-text config must be a mapping")
    case = request.config.get("case", "preserve")
    if case not in {"lower", "preserve", "upper"}:
        raise ValueError("normalize-text case must be lower, preserve, or upper")
    prefix = request.config.get("prefix", "")
    if not isinstance(prefix, str):
        raise TypeError("normalize-text prefix must be a string")

    normalized = " ".join(text.split())
    if case == "lower":
        normalized = normalized.lower()
    elif case == "upper":
        normalized = normalized.upper()
    return WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=request.lease_epoch,
        outputs={"text": f"{prefix}{normalized}"},
    )
