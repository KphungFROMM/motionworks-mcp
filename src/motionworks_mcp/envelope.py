"""The result envelope every tool returns.

One shape for every answer, so a caller never has to learn a second error story.
The three kinds below are kept strictly apart and are never conflated:

``findings``
    Something is wrong with the subject matter. The attempt ran.
``warnings``
    The attempt ran, but part of the answer is missing or degraded. Results are
    still returned.
``normalisations``
    Input was rewritten to be checkable, and here is exactly what changed.

Nothing in this module refuses. A caller decides what a finding means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ordered worst-first; a summary takes the highest severity present.
SEVERITIES = ("error", "warning", "info")


@dataclass
class Diagnostic:
    """One finding, warning or normalisation."""

    code: str
    message: str
    severity: str = "info"
    location: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.location:
            out["location"] = self.location
        return out


@dataclass
class Result:
    """A tool answer: data plus everything the caller should know about it."""

    data: Any = None
    findings: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    normalisations: list[Diagnostic] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def find(self, code: str, message: str, location: str | None = None) -> None:
        self.findings.append(Diagnostic(code, message, "error", location))

    def warn(self, code: str, message: str, location: str | None = None) -> None:
        self.warnings.append(Diagnostic(code, message, "warning", location))

    def note(self, code: str, message: str, location: str | None = None) -> None:
        self.warnings.append(Diagnostic(code, message, "info", location))

    def normalised(self, code: str, message: str, location: str | None = None) -> None:
        self.normalisations.append(Diagnostic(code, message, "info", location))

    def set(self, key: str, value: Any) -> None:
        self.meta[key] = value

    @property
    def ok(self) -> bool:
        """True when nothing was reported at error severity."""
        return not any(item.severity == "error" for item in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "findings": [item.as_dict() for item in self.findings],
            "warnings": [item.as_dict() for item in self.warnings],
            "normalisations": [item.as_dict() for item in self.normalisations],
            "ok": self.ok,
            "meta": self.meta,
        }


def disabled(capability: str, flag: str, detail: str) -> dict[str, Any]:
    """The typed answer for a capability that is switched off.

    This is a configuration decision reported as such. It is not an error, not a
    refusal, and never a partial action.
    """
    return {
        "data": None,
        "findings": [],
        "warnings": [],
        "normalisations": [],
        "ok": True,
        "meta": {
            "capability": capability,
            "capability_disabled": True,
            "enable_with": flag,
            "detail": detail,
        },
    }
