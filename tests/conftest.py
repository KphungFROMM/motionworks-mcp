"""Shared fixtures: locate the project corpus and the package under test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The corpus is the four real projects recovered from the ASP library. Tests skip
# rather than fail when it is absent, so the suite is meaningful on any machine.
CORPUS_ENV = "MOTIONWORKS_CORPUS"
DEFAULT_CORPUS = Path(r"C:\DeepseekAI\AryaAIDSH\_scratch\yaskawa-projects\Yaskawa")

PROJECTS = {
    "topcutter": "MP2600iec Program/TopCutter",
    "topcutter_alt": "TopCutter",
    "rotaryknife": "RotaryKnife_ASP_v350",
    "rk_mp3300": "RK_DemoOnMP3300iec",
    "rk_mp2300": "RK_DemoOnMP2300Siec",
}


def corpus_root() -> Path:
    return Path(os.environ.get(CORPUS_ENV, DEFAULT_CORPUS))


@pytest.fixture(scope="session")
def corpus() -> Path:
    root = corpus_root()
    if not root.is_dir():
        pytest.skip(f"project corpus not present at {root}")
    return root


@pytest.fixture(scope="session")
def project_paths(corpus: Path) -> dict[str, Path]:
    found = {name: corpus / rel for name, rel in PROJECTS.items()}
    present = {name: path for name, path in found.items() if path.is_dir()}
    if not present:
        pytest.skip("none of the corpus projects are present")
    return present


@pytest.fixture
def topcutter(project_paths: dict[str, Path]) -> Path:
    if "topcutter" not in project_paths:
        pytest.skip("the TopCutter-with-source project is not present")
    return project_paths["topcutter"]
