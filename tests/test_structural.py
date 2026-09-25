"""Structural validation: the checks that a round trip through this package cannot make.

The defects that reached a real build all passed a structural round trip, because
`CompoundFile` walks the DIFAT and a wrong header count is invisible to that. These
tests fix the validator against containers MotionWorks itself wrote, and against
deliberately damaged ones it must reject.
"""

from __future__ import annotations

import struct
import shutil
from pathlib import Path

import pytest

from motionworks_mcp.cfb_write import CompoundWriter
from motionworks_mcp.structural import assert_clean, validate_container

HEADER_FAT_COUNT = 44


@pytest.fixture
def container(topcutter: Path, tmp_path: Path) -> Path:
    source = topcutter / "POE" / "TopCutterCutControl" / "src.st1"
    if not source.is_file():
        pytest.skip("the TopCutter POU container is not present")
    work = tmp_path / "Probe"
    shutil.copytree(topcutter / "POE" / "TopCutterCutControl", work)
    return work / "src.st1"


def test_motionworks_own_containers_validate_clean(topcutter: Path) -> None:
    """The validator must agree with the vendor's own output, or it is measuring itself.

    Every container MotionWorks wrote in this project has to pass. A validator that
    reports findings on correct input is worse than none: it trains the reader to
    ignore it.
    """
    containers = sorted(topcutter.rglob("*.st1")) + sorted(topcutter.rglob("*.sto"))
    assert containers
    for path in containers:
        report = validate_container(path)
        assert_clean(report, str(path.relative_to(topcutter)))


def test_a_written_container_validates_clean(container: Path) -> None:
    """Everything this writer produces must pass the vendor containers' check."""
    for size in (100, 3000, 40000, 200000):
        writer = CompoundWriter(container)
        writer.replace_stream("TopCutterCutControl.STB", b"a" * size)
        writer.save()
        assert_clean(validate_container(container), f"after writing {size} bytes")


def test_a_wrong_header_fat_count_is_caught(container: Path) -> None:
    """The defect that reached a real build.

    Adding FAT sectors without updating the header's count leaves every sector past
    the declared ones unreachable. Our own reader follows the DIFAT and read the
    broken container happily, which is why this check exists.
    """
    writer = CompoundWriter(container)
    writer.replace_stream("TopCutterCutControl.STB", b"b" * 200000)
    writer.save()
    assert_clean(validate_container(container))

    data = bytearray(container.read_bytes())
    declared = struct.unpack_from("<I", data, HEADER_FAT_COUNT)[0]
    assert declared > 1, "this test needs a container with more than one FAT sector"
    struct.pack_into("<I", data, HEADER_FAT_COUNT, 1)
    container.write_bytes(bytes(data))

    report = validate_container(container)
    assert not report.ok
    assert any("header declares" in finding for finding in report.findings)


def test_a_chain_shorter_than_the_declared_size_is_caught(container: Path) -> None:
    """A directory entry promising more bytes than its chain holds."""
    data = bytearray(container.read_bytes())
    for offset in range(512, len(data) - 127, 128):
        name_len = struct.unpack_from("<H", data, offset + 64)[0]
        if not (2 < name_len <= 128):
            continue
        name = bytes(data[offset : offset + name_len - 2]).decode("utf-16-le", "replace")
        if name.endswith(".STB"):
            size = struct.unpack_from("<I", data, offset + 120)[0]
            struct.pack_into("<I", data, offset + 120, size + 100000)
            break
    else:
        pytest.skip("no body stream in this container")
    container.write_bytes(bytes(data))

    report = validate_container(container)
    assert not report.ok
    assert any("chain holds" in finding for finding in report.findings)


def test_a_missing_mini_fat_is_caught(topcutter: Path, tmp_path: Path) -> None:
    """An entry that must live in the mini stream, with no mini stream to point at.

    Needs a container that genuinely uses the mini stream. `TopCutterCutControl` has
    none — every one of its streams is above the cutoff and its root stream is empty
    — so damaging the mini FAT there changes nothing and proves nothing. A POU whose
    declarations are small does use it.
    """
    candidate = None
    for directory in sorted((topcutter / "POE").iterdir()):
        source = directory / "src.st1"
        if not source.is_file():
            continue
        report = validate_container(source)
        if any(size < 4096 for size in report.streams.values()):
            candidate = source
            break
    if candidate is None:
        pytest.skip("no POU in this project stores a stream in the mini stream")

    work = tmp_path / "Mini"
    shutil.copytree(candidate.parent, work)
    target = work / "src.st1"
    assert_clean(validate_container(target), "before damage")

    data = bytearray(target.read_bytes())
    struct.pack_into("<I", data, 60, 0xFFFFFFFE)  # mini FAT start -> end of chain
    target.write_bytes(bytes(data))

    report = validate_container(target)
    assert not report.ok, "a container whose mini chains cannot resolve must not pass"
    assert any(
        "mini" in finding or "chain" in finding for finding in report.findings
    ), report.findings


def test_a_damaged_container_is_reported_not_raised(tmp_path: Path) -> None:
    """Validation answers; it does not throw at the caller."""
    junk = tmp_path / "not-a-container.st1"
    junk.write_bytes(b"plain text, no signature")
    report = validate_container(junk)
    assert not report.ok
    assert report.findings


def test_a_truncated_container_is_reported(tmp_path: Path) -> None:
    truncated = tmp_path / "short.st1"
    truncated.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    report = validate_container(truncated)
    assert not report.ok


def test_the_report_names_what_it_checked(container: Path) -> None:
    report = validate_container(container)
    assert report.sector_size == 512
    assert report.mini_sector_size == 64
    assert report.sectors > 0
    assert report.fat_sectors_declared == report.fat_sectors_named
    assert report.streams, "the four POU streams should be enumerated"
    assert report.as_dict()["ok"] is True
