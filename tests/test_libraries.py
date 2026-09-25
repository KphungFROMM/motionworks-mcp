"""Library path resolution: reporting a revision mismatch, and retargeting it."""

from __future__ import annotations

from pathlib import Path

from motionworks_mcp.envelope import Result
from motionworks_mcp.libraries import (
    LibraryEntry,
    find_compatible,
    installed_libraries,
    parse_library_list,
    resolve_libraries,
    retarget_library_paths,
)

SAMPLE = """Library List, V40
FW;C:\\ProgramData\\Yaskawa\\MotionWorks IEC 3 Pro\\3_7_5_1_667\\plc\\FW_LIB\\YMotion;YMotion;3
USER;C:\\Libraries\\Cam_Toolbox_v375;LIST;0
USER;C:\\Libraries\\FileRW_Toolbox_v374;LIST;0
"""


def test_parse_skips_the_header_and_keeps_membership_names() -> None:
    entries = parse_library_list(SAMPLE)
    assert len(entries) == 3
    assert entries[0].class_name == "FW"
    assert entries[0].member == "YMotion"
    assert entries[1].folder == "Cam_Toolbox_v375"
    assert entries[1].member == "LIST"


def test_parse_survives_a_malformed_line() -> None:
    """A line with too few fields is skipped, not treated as a library."""
    entries = parse_library_list("Library List, V40\nonly;two\nA;B;C\n")
    assert [entry.class_name for entry in entries] == ["A"]


def test_find_compatible_matches_the_same_library_only() -> None:
    """A different revision is a substitute; a different library is not."""
    available = {
        "Cam_Toolbox_v374": Path(r"C:\Libraries\Cam_Toolbox_v374"),
        "Math_Toolbox_v374": Path(r"C:\Libraries\Math_Toolbox_v374"),
    }
    assert find_compatible("Cam_Toolbox_v375", available) == (
        "Cam_Toolbox_v374",
        Path(r"C:\Libraries\Cam_Toolbox_v374"),
    )
    assert find_compatible("Yaskawa_Toolbox_v375", available) is None


def test_find_compatible_prefers_an_exact_match() -> None:
    available = {
        "Cam_Toolbox_v374": Path(r"C:\a"),
        "Cam_Toolbox_v375": Path(r"C:\b"),
    }
    assert find_compatible("Cam_Toolbox_v375", available) == ("Cam_Toolbox_v375", Path(r"C:\b"))


def test_find_compatible_picks_the_newest_available_revision() -> None:
    available = {
        "Cam_Toolbox_v370": Path(r"C:\a"),
        "Cam_Toolbox_v374": Path(r"C:\b"),
        "Cam_Toolbox_v372": Path(r"C:\c"),
    }
    assert find_compatible("Cam_Toolbox_v375", available)[0] == "Cam_Toolbox_v374"


def test_a_name_without_a_revision_is_not_substituted() -> None:
    available = {"SomethingElse": Path(r"C:\x")}
    assert find_compatible("NoRevisionHere", available) is None


def test_retarget_rewrites_the_path_and_keeps_the_member(tmp_path: Path, monkeypatch) -> None:
    """Only the directory moves; the project's record of the library is unchanged.

    The available set is injected so the test describes the behaviour rather than
    whatever happens to be installed on the machine running it.
    """
    import motionworks_mcp.libraries as libraries_module

    project = tmp_path / "Proj"
    project.mkdir()
    stand_in = tmp_path / "Cam_Toolbox_v374"
    stand_in.mkdir()
    monkeypatch.setattr(
        libraries_module, "installed_libraries", lambda: {"Cam_Toolbox_v374": stand_in}
    )

    listing = project / "@LIBRARY.LST"
    listing.write_text(
        f"Library List, V40\nUSER;{tmp_path / 'Cam_Toolbox_v375'};LIST;0\n",
        encoding="latin-1",
    )

    result = Result()
    substitutions = retarget_library_paths(project, result)

    assert substitutions == ["Cam_Toolbox_v375 -> Cam_Toolbox_v374"]
    body = listing.read_text(encoding="latin-1")
    assert str(stand_in) in body
    assert body.rstrip().endswith(";LIST;0")
    assert [item.code for item in result.normalisations] == ["library_path_retargeted"]


def test_an_exact_match_is_never_rewritten(tmp_path: Path, monkeypatch) -> None:
    """When the declared revision is installed, the path is left alone."""
    import motionworks_mcp.libraries as libraries_module

    project = tmp_path / "Proj"
    project.mkdir()
    installed = tmp_path / "Cam_Toolbox_v375"
    installed.mkdir()
    monkeypatch.setattr(
        libraries_module, "installed_libraries", lambda: {"Cam_Toolbox_v375": installed}
    )
    listing = project / "@LIBRARY.LST"
    listing.write_text(
        f"Library List, V40\nUSER;{installed};LIST;0\n", encoding="latin-1"
    )
    result = Result()
    assert retarget_library_paths(project, result) == []
    assert result.normalisations == []


def test_retarget_leaves_an_already_valid_path_alone(tmp_path: Path) -> None:
    project = tmp_path / "Proj"
    project.mkdir()
    existing = tmp_path / "Some_Library_v1"
    existing.mkdir()
    listing = project / "@LIBRARY.LST"
    body = f"Library List, V40\nUSER;{existing};LIST;0\n"
    listing.write_text(body, encoding="latin-1")
    result = Result()
    assert retarget_library_paths(project, result) == []
    assert listing.read_text(encoding="latin-1") == body


def test_resolve_reports_differing_revision_rather_than_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """A project saved against another revision is described, not refused."""
    import motionworks_mcp.libraries as libraries_module

    available = {"Cam_Toolbox_v374": tmp_path / "Cam_Toolbox_v374"}
    available["Cam_Toolbox_v374"].mkdir()
    monkeypatch.setattr(libraries_module, "installed_libraries", lambda: available)

    project = tmp_path / "Proj"
    project.mkdir()
    (project / "@LIBRARY.LST").write_text(
        "Library List, V40\nUSER;C:\\nope\\Cam_Toolbox_v375;LIST;0\n", encoding="latin-1"
    )
    result = Result()
    rows = resolve_libraries(project, result)
    assert rows[0]["status"] == "different_revision"
    assert rows[0]["compatible"] == "Cam_Toolbox_v374"
    codes = {warning.code for warning in result.warnings}
    assert "library_revision_differs" in codes
    assert "library_missing" not in codes


def test_library_entry_reports_resolution(tmp_path: Path) -> None:
    existing = tmp_path / "Lib_v1"
    existing.mkdir()
    assert LibraryEntry("USER", str(existing), "LIST", "0").resolves is True
    assert LibraryEntry("USER", str(tmp_path / "nope"), "LIST", "0").resolves is False
