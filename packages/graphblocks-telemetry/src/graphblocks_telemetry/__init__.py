from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


class TelemetryProjectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationTelemetryRecord:
    record_id: str
    run_id: str
    span_id: str
    node_id: str
    provider: str
    model: str
    release_id: str | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    timing_ms: Mapping[str, int] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        object.__setattr__(self, "timing_ms", MappingProxyType(dict(self.timing_ms)))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def observation_contract(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "node_id": self.node_id,
            "provider": self.provider,
            "model": self.model,
            "release_id": self.release_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "usage": dict(sorted(self.usage.items())),
            "timing_ms": dict(sorted(self.timing_ms.items())),
            "attributes": dict(sorted(self.attributes.items())),
        }


@dataclass(frozen=True, slots=True)
class TelemetryExportResult:
    exporter: str
    status: str
    record_ids: tuple[str, ...]
    error_type: str | None = None
    retryable: bool = False
    run_impact: str = "none"

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
        if self.run_impact != "none":
            raise TelemetryProjectionError("telemetry export result must not affect run correctness")

    @classmethod
    def failed(
        cls,
        *,
        exporter: str,
        record_ids: tuple[str, ...],
        error_type: str,
        retryable: bool,
    ) -> TelemetryExportResult:
        return cls(
            exporter=exporter,
            status="failed",
            record_ids=record_ids,
            error_type=error_type,
            retryable=retryable,
            run_impact="none",
        )

    def result_contract(self) -> dict[str, object]:
        return {
            "exporter": self.exporter,
            "status": self.status,
            "record_ids": list(self.record_ids),
            "error_type": self.error_type,
            "retryable": self.retryable,
            "run_impact": self.run_impact,
        }


__all__ = [
    "GenerationTelemetryRecord",
    "TelemetryExportResult",
    "TelemetryProjectionError",
]
