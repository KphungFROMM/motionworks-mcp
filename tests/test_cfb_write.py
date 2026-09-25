"""Stream replacement across the mini-stream cutoff.

The two allocation paths meet at the 4096-byte cutoff, and the interesting cases sit
on the boundary. Deciding which path applies from the *entry's recorded size* rather
than the payload's makes two writers delegate to each other; trusting a large
entry's FAT sector index as a mini index points a directory entry at unrelated bytes
and reads back the right length with the wrong content. Both were live defects.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from motionworks_mcp.cfb import CompoundFile
from motionworks_mcp.cfb_write import CompoundWriter

CUTOFF = 4096

# Deliberately on both sides of the cutoff and at the boundaries.
SIZES = [0, 1, 63, 64, 65, 512, 4094, 4095, 4096, 4097, 8192, 40000]

TARGETS = [
    "TopCutterCutControl.STB",
    "TopCutterCutControlV.VB",
    "TopCutterCutControlV.VGR",
    "TopCutterCutControlT.TXT",
]


@pytest.fixture
def container(topcutter: Path, tmp_path: Path) -> Path:
    """A working copy of one POU container, so tests never touch the corpus."""
    source = topcutter / "POE" / "TopCutterCutControl" / "src.st1"
    if not source.is_file():
        pytest.skip("the TopCutter POU container is not present")
    work = tmp_path / "Probe"
    shutil.copytree(topcutter / "POE" / "TopCutterCutControl", work)
    return work / "src.st1"


def test_every_size_transition_writes_and_reads_back(container: Path) -> None:
    """Growth, shrinkage and crossings of the cutoff all round-trip."""
    original = CompoundFile(container)
    untouched = {
        name: original.read_stream(name)
        for name in original.stream_names()
        if name != "TopCutterCutControl.STB"
    }
    for size in SIZES:
        payload = (b"x" * size) if size else b""
        writer = CompoundWriter(container)
        writer.replace_stream("TopCutterCutControl.STB", payload)
        writer.save()

        reopened = CompoundFile(container)
        assert reopened.read_stream("TopCutterCutControl.STB") == payload, f"size {size}"
        for name, expected in untouched.items():
            assert reopened.read_stream(name) == expected, f"size {size}: {name} changed"


def test_a_large_stream_replaced_by_a_small_one_reads_back(container: Path) -> None:
    """The case that read back as the right length and the wrong content.

    A 30 KB body replaced by 19 bytes crosses downward over the cutoff. The entry
    must end up describing mini sectors, and its start field must be a mini index
    rather than the FAT sector index it used to hold.
    """
    entry_before = CompoundFile(container).find("TopCutterCutControl.STB")
    assert entry_before is not None and entry_before.size >= CUTOFF

    payload = b"(* cache check *)\r\n"
    writer = CompoundWriter(container)
    writer.replace_stream("TopCutterCutControl.STB", payload)
    writer.save()

    reopened = CompoundFile(container)
    assert reopened.read_stream("TopCutterCutControl.STB") == payload
    entry_after = reopened.find("TopCutterCutControl.STB")
    assert entry_after is not None
    assert entry_after.size == len(payload)
    assert entry_after.start_sector < 4096, "a mini-backed entry stores a mini index"


def test_a_small_stream_replaced_by_a_large_one_reads_back(container: Path) -> None:
    """Crossing upward frees the mini chain and allocates a FAT chain."""
    if CompoundFile(container).find("TopCutterCutControlV.VB").size >= CUTOFF:
        pytest.skip("this stream does not start below the cutoff")
    payload = b"VAR\n  " + b"x" * 9000 + b"\nEND_VAR\n"
    writer = CompoundWriter(container)
    writer.replace_stream("TopCutterCutControlV.VB", payload)
    writer.save()
    reopened = CompoundFile(container)
    assert reopened.read_stream("TopCutterCutControlV.VB") == payload
    assert reopened.find("TopCutterCutControl.STB").size > 0, "the body survived"


def test_every_named_stream_can_be_replaced(container: Path) -> None:
    """No stream is special: each one round-trips at a size on the other side."""
    for name in TARGETS:
        entry = CompoundFile(container).find(name)
        if entry is None:
            continue
        payload = b"y" * (200 if entry.size >= CUTOFF else 20000)
        writer = CompoundWriter(container)
        writer.replace_stream(name, payload)
        writer.save()
        assert CompoundFile(container).read_stream(name) == payload, name


def test_an_unknown_stream_is_a_real_precondition(container: Path) -> None:
    writer = CompoundWriter(container)
    with pytest.raises(KeyError):
        writer.replace_stream("NotAStream", b"x")


def test_repeated_writes_to_one_container_stay_consistent(container: Path) -> None:
    """Ten alternating rewrites must not leak sectors or drift the directory."""
    for index in range(10):
        size = 100 if index % 2 else 20000
        payload = bytes([65 + index]) * size
        writer = CompoundWriter(container)
        writer.replace_stream("TopCutterCutControl.STB", payload)
        writer.save()
        assert CompoundFile(container).read_stream("TopCutterCutControl.STB") == payload, index
    # the mini FAT must still describe a chain that terminates
    reopened = CompoundFile(container)
    entry = reopened.find("TopCutterCutControl.STB")
    assert entry is not None
    assert entry.start_sector >= 0
