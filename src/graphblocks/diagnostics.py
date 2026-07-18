from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    path: str = "$"
    severity: Severity = "error"

    def __post_init__(self) -> None:
        if self.severity not in ("error", "warning", "info"):
            raise ValueError(f"diagnostic severity has invalid value {self.severity!r}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticSet:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def to_list(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.diagnostics]
