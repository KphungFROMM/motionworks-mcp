"""Validate every container in every corpus project.

Two purposes: prove the validator agrees with containers MotionWorks itself wrote,
and find anything in the corpus that no test has covered yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from motionworks_mcp.structural import validate_container  # noqa: E402

CORPUS = Path(r"C:\DeepseekAI\AryaAIDSH\_scratch\yaskawa-projects\Yaskawa")


def main() -> int:
    containers = sorted(CORPUS.rglob("*.st1")) + sorted(CORPUS.rglob("*.sto")) + sorted(
        CORPUS.rglob("*.mwt")
    )
    if not containers:
        print(f"no containers under {CORPUS}")
        return 1

    checked = bad = warned = 0
    by_project: dict[str, list[str]] = {}
    for path in containers:
        report = validate_container(path)
        checked += 1
        if not report.ok:
            bad += 1
            by_project.setdefault(str(path.relative_to(CORPUS)), list(report.findings))
        elif report.warnings:
            warned += 1
            by_project.setdefault(str(path.relative_to(CORPUS)), list(report.warnings))

    print(f"validated {checked} container(s): {checked - bad - warned} clean, {warned} with warnings, {bad} with findings")
    print()
    for name in sorted(by_project):
        print(f"  {name}")
        for item in by_project[name][:6]:
            print(f"      {item[:150]}")
        if len(by_project[name]) > 6:
            print(f"      ... and {len(by_project[name]) - 6} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
