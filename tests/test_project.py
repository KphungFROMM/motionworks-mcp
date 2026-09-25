"""Behaviour of the CFB reader and the project model against real projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from motionworks_mcp.cfb import CfbError, CompoundFile
from motionworks_mcp.project import Project, iec_blocks


def test_reader_rejects_a_non_container(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-container.st1"
    plain.write_bytes(b"just text, no CFB header")
    with pytest.raises(CfbError, match="not a compound file"):
        CompoundFile(plain)


def test_reader_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CfbError, match="cannot read container"):
        CompoundFile(tmp_path / "absent.st1")


def test_reader_opens_every_container_in_every_project(project_paths: dict[str, Path]) -> None:
    """The reader must not be defeated by any project generation in the corpus."""
    containers = 0
    for name, path in project_paths.items():
        for st1 in path.rglob("*.st1"):
            container = CompoundFile(st1)
            assert container.sector_size == 512, f"{name}: {st1}"
            containers += 1
    assert containers > 0


def test_every_project_reports_its_pous(project_paths: dict[str, Path]) -> None:
    for name, path in project_paths.items():
        project = Project(path)
        assert project.pous, f"{name}: no POUs found"
        assert project.directory.is_dir()


def test_topcutter_target_and_generation(topcutter: Path) -> None:
    project = Project(topcutter)
    hardware = project.hardware()
    assert hardware["firmware_model"] == "MP2600iec"
    assert hardware["ide_version"] == "3.7.5.1"


def test_older_project_still_loads(project_paths: dict[str, Path]) -> None:
    """IDE 1.2.x puts the resource somewhere else entirely; that must not matter."""
    if "rotaryknife" not in project_paths:
        pytest.skip("the v350 project is not present")
    project = Project(project_paths["rotaryknife"])
    assert project.hardware().get("ide_version") == "1.2.1.1"
    assert len(project.pous) >= 8


def test_project_library_list_is_parsed(topcutter: Path) -> None:
    """Every declared dependency is reported, firmware libraries included.

    The third field is the library's membership name, which for a toolbox is the
    literal ``LIST`` rather than the versioned folder name, so the folder is what
    distinguishes one toolbox from another.
    """
    libraries = Project(topcutter).library_list()
    names = {entry["name"] for entry in libraries}
    paths = " ".join(entry["path"] for entry in libraries)
    assert {"PLCopenPlus_v_2_2a", "YMotion", "eCLR"} <= names
    assert "PLCopen_Toolbox_v375" in paths
    assert all("installed" in entry for entry in libraries)


def test_globals_and_addresses_are_both_recovered(topcutter: Path) -> None:
    """The declaration set and the address map must agree; neither is dropped."""
    project = Project(topcutter)
    text, addresses = project.global_variables()
    assert len(text) > 8000
    assert len(addresses) > 100
    assert all(row["address"].startswith("%") for row in addresses)


def test_variables_keep_their_direct_addresses(topcutter: Path) -> None:
    from motionworks_mcp.pou import parse_declarations

    project = Project(topcutter)
    text, addresses = project.global_variables()
    variables = parse_declarations(text)
    with_address = [variable for variable in variables if variable.address]
    assert len(with_address) == len(addresses)
    assert any(variable.name == "PLC_SYS_TICK_CNT" for variable in variables)


def test_comments_are_not_folded_into_the_next_declaration(topcutter: Path) -> None:
    """A comment after the semicolon must not swallow the following variable.

    This is the defect that silently halved the recovered declaration set.
    """
    from motionworks_mcp.pou import parse_declarations

    project = Project(topcutter)
    text, _addresses = project.global_variables()
    names = {variable.name for variable in parse_declarations(text)}
    # these three follow commented declarations in the file
    assert {"PLCMODE_ON", "PLCMODE_LOADING", "PLCMODE_STOP"} <= names


def test_tasks_and_axes_are_read(topcutter: Path) -> None:
    project = Project(topcutter)
    tasks = {task["name"] for task in project.tasks()}
    assert {"FastTsk", "MedTsk", "SlowTsk", "Start"} <= tasks
    assert len(project.axes()) == 2


def test_io_configuration_is_read(topcutter: Path) -> None:
    io = Project(topcutter).io()
    assert io.get("t2o", {}).get("instance") == "101"
    assert io.get("o2t", {}).get("instance") == "111"


def test_iec_blocks_ignores_binary_payloads() -> None:
    assert iec_blocks(b"\x00\x01\x02\x03" * 40) == []


def test_iec_blocks_normalises_doubled_carriage_returns() -> None:
    """Payload text arrives with CRLF written into a CRLF file; it is normalised."""
    payload = (
        b"VAR_GLOBAL\r\r\n"
        b"\tPLC_TASK_DEFINED AT %MW1.4 : INT;\r\r\n"
        b"\tPLCMODE_RUN AT %MX1.7.0 : BOOL;\r\r\n"
        b"END_VAR\r\r\n"
    )
    blocks = iec_blocks(payload, min_run=20)
    assert blocks
    assert "\r" not in blocks[0]
    assert "PLC_TASK_DEFINED" in blocks[0]


def test_unfamiliar_layout_is_reported_not_refused(tmp_path: Path) -> None:
    """A project with nothing recognisable still answers, with warnings."""
    empty = tmp_path / "Empty"
    empty.mkdir()
    project = Project(empty)
    assert project.pous == []
    codes = {warning.code for warning in project.result.warnings}
    assert "resource_missing" in codes
    assert "poe_missing" in codes
