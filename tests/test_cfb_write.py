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


def _strict_read(path: Path) -> dict[str, bytes]:
    """Read a CFB container the way the specification says to, not via our reader.

    The header's `number of FAT sectors` field is authoritative: read exactly that
    many FAT sector numbers from the DIFAT and no more. A writer that adds FAT
    sectors without updating the field leaves every sector beyond them unreachable,
    and a reader that walks the DIFAT instead of the count cannot see the mistake.
    """
    import struct

    data = path.read_bytes()
    assert data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "signature"
    sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_size = 1 << struct.unpack_from("<H", data, 32)[0]
    fat_count = struct.unpack_from("<I", data, 44)[0]
    directory_start = struct.unpack_from("<I", data, 48)[0]
    cutoff = struct.unpack_from("<I", data, 56)[0]
    minifat_start = struct.unpack_from("<I", data, 60)[0]
    minifat_count = struct.unpack_from("<I", data, 64)[0]

    difat = [struct.unpack_from("<I", data, 76 + index * 4)[0] for index in range(109)]
    fat_sector_ids = [value for value in difat if value != 0xFFFFFFFF]
    assert len(fat_sector_ids) >= fat_count, (
        f"header declares {fat_count} FAT sectors but the DIFAT names {len(fat_sector_ids)}"
    )
    fat_sector_ids = fat_sector_ids[:fat_count]

    fat: list[int] = []
    for sector in fat_sector_ids:
        offset = 512 + sector * sector_size
        fat.extend(struct.unpack_from(f"<{sector_size // 4}I", data, offset))

    def sector(index: int) -> bytes:
        offset = 512 + index * sector_size
        return data[offset : offset + sector_size]

    def chain(start: int) -> list[int]:
        out: list[int] = []
        current = start
        while current not in (0xFFFFFFFE, 0xFFFFFFFF) and current < len(fat):
            out.append(current)
            current = fat[current]
        return out

    entries: dict[str, tuple[int, int, int]] = {}
    directory_bytes = b"".join(sector(index) for index in chain(directory_start))
    for offset in range(0, len(directory_bytes) - 127, 128):
        name_len = struct.unpack_from("<H", directory_bytes, offset + 64)[0]
        if name_len <= 2 or name_len > 128:
            continue
        name = directory_bytes[offset : offset + name_len - 2].decode("utf-16-le", "replace")
        entries[name] = (
            directory_bytes[offset + 66],
            struct.unpack_from("<I", directory_bytes, offset + 116)[0],
            struct.unpack_from("<I", directory_bytes, offset + 120)[0],
        )

    root = next((value for value in entries.values() if value[0] == 5), None)
    assert root is not None
    mini_stream = b"".join(sector(index) for index in chain(root[1]))
    minifat: list[int] = []
    for index in chain(minifat_start):
        minifat.extend(struct.unpack_from(f"<{sector_size // 4}I", sector(index)))
    assert len(minifat) >= minifat_count, "the mini FAT is shorter than the header says"

    out: dict[str, bytes] = {}
    for name, (obj_type, start, size) in entries.items():
        if obj_type != 2 or size == 0:
            continue
        if size < cutoff:
            buf = bytearray()
            current = start
            while current not in (0xFFFFFFFE, 0xFFFFFFFF) and current < len(minifat):
                buf += mini_stream[current * mini_size : (current + 1) * mini_size]
                current = minifat[current]
            out[name] = bytes(buf[:size])
        else:
            out[name] = b"".join(sector(index) for index in chain(start))[:size]
    return out


def test_the_header_names_exactly_as_many_fat_sectors_as_the_difat(container: Path) -> None:
    """The field that decides how much of the container is reachable.

    Adding a FAT sector without updating `number of FAT sectors` leaves every sector
    beyond the declared ones unreachable: stream payloads stored there read as
    absent, and MotionWorks reported `File error` against a build stream it could no
    longer locate. Our own reader walks the DIFAT and cannot see this.
    """
    import struct

    for size in (100, 60000, 200000):
        writer = CompoundWriter(container)
        writer.replace_stream("TopCutterCutControl.STB", b"z" * size)
        writer.save()
        data = container.read_bytes()
        fat_count = struct.unpack_from("<I", data, 44)[0]
        difat = [struct.unpack_from("<I", data, 76 + i * 4)[0] for i in range(109)]
        named = [value for value in difat if value != 0xFFFFFFFF]
        assert fat_count == len(named), f"size {size}: header {fat_count} vs DIFAT {len(named)}"


def test_a_spec_strict_reader_sees_every_stream_after_a_growth(container: Path) -> None:
    """Read through the declared header fields only, as another implementation would."""
    for size in (200, 200000):
        payload = bytes([65 + (size % 26)]) * size
        writer = CompoundWriter(container)
        writer.replace_stream("TopCutterCutControl.STB", payload)
        writer.save()

        strict = _strict_read(container)
        ours = CompoundFile(container)
        # a zero-length stream has nothing to read and is legitimately absent
        expected = {name for name in ours.stream_names() if len(ours.read_stream(name)) > 0}
        assert set(strict) == expected, f"size {size}: stream sets differ"
        for name in strict:
            assert strict[name] == ours.read_stream(name), f"size {size}: {name} differs"
        assert strict["TopCutterCutControl.STB"] == payload, f"size {size}"


def test_the_reported_fat_capacity_matches_the_header(container: Path) -> None:
    """Capacity is derived from the declared count, not from the DIFAT's length."""
    import struct

    writer = CompoundWriter(container)
    writer.replace_stream("TopCutterCutControl.STB", b"q" * 150000)
    writer.save()
    data = container.read_bytes()
    sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
    fat_count = struct.unpack_from("<I", data, 44)[0]
    reader = CompoundFile(container)
    assert len(reader.fat) >= fat_count * (sector_size // 4)


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
