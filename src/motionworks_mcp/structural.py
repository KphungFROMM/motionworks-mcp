"""Structural validation of a compound file, against the format and itself.

Written because three defects reached a real build while a structural round trip
passed every time. The reason is that :class:`~motionworks_mcp.cfb.CompoundFile`
walks the DIFAT and trusts it, so a container whose *header* is wrong reads back
perfectly through this package and fails in any other implementation. A validator
that only asks "can I read it back" cannot find that class of defect.

So this checks the container the way another implementation would, and it checks
that the parts agree with each other:

* the header's declared counts match the DIFAT and the chains;
* every reachable sector is accounted for — allocated, part of a chain, or a
  structure sector — so nothing is silently orphaned;
* every directory entry's chain is long enough for the size it claims;
* no two allocations overlap;
* the mini and normal spaces do not leak into each other across the cutoff.

Reported as findings rather than exceptions: a container with problems is still
returned, because the caller usually wants to know how much is readable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .cfb import (
    DIFSECT,
    ENDOFCHAIN,
    FATSECT,
    FREESECT,
    CfbError,
)

HEADER_SIZE = 512
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass
class StructuralReport:
    """What validation found."""

    path: Path | None = None
    sector_size: int = 0
    mini_sector_size: int = 0
    sectors: int = 0
    fat_sectors_declared: int = 0
    fat_sectors_named: int = 0
    free_sectors: int = 0
    streams: dict[str, int] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "sector_size": self.sector_size,
            "sectors": self.sectors,
            "fat_sectors_declared": self.fat_sectors_declared,
            "fat_sectors_named": self.fat_sectors_named,
            "free_sectors": self.free_sectors,
            "streams": dict(self.streams),
            "findings": list(self.findings),
            "warnings": list(self.warnings),
            "ok": self.ok,
        }


def validate_container(path: str | Path) -> StructuralReport:
    """Check one compound file. Never raises for a malformed container."""
    path = Path(path)
    report = StructuralReport(path=path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        report.findings.append(f"cannot read file: {exc}")
        return report

    if len(data) < HEADER_SIZE or data[:8] != MAGIC:
        report.findings.append("not a compound file: bad signature or truncated header")
        return report

    sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_size = 1 << struct.unpack_from("<H", data, 32)[0]
    fat_count = struct.unpack_from("<I", data, 44)[0]
    dir_start = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    minifat_start = struct.unpack_from("<I", data, 60)[0]
    minifat_count = struct.unpack_from("<I", data, 64)[0]
    difat_start = struct.unpack_from("<I", data, 68)[0]
    difat_count = struct.unpack_from("<I", data, 72)[0]
    report.sector_size = sector_size
    report.mini_sector_size = mini_size
    report.sectors = (len(data) - HEADER_SIZE) // sector_size

    if mini_size != 64:
        report.warnings.append(f"unusual mini sector size: {mini_size}")
    if mini_cutoff != 4096:
        report.warnings.append(f"unusual mini stream cutoff: {mini_cutoff}")

    # -- the DIFAT and the declared count must agree ----------------------
    difat: list[int] = []
    for index in range(109):
        value = struct.unpack_from("<I", data, 76 + index * 4)[0]
        if value != FREESECT:
            difat.append(value)
    if difat_start not in (ENDOFCHAIN, FREESECT) and difat_count:
        # a DIFAT sector chain would be read here; these containers never need one
        report.warnings.append(
            f"a DIFAT sector chain is declared ({difat_count} sector(s) at {difat_start}); "
            "this validator reads the header slots only"
        )
    report.fat_sectors_declared = fat_count
    report.fat_sectors_named = len(difat)
    if fat_count != len(difat):
        report.findings.append(
            f"header declares {fat_count} FAT sector(s) but the DIFAT names {len(difat)}; "
            "a reader that trusts the header cannot reach sectors past the declared ones"
        )
    if fat_count == 0:
        report.findings.append("header declares zero FAT sectors")
        return report
    for sector in difat:
        if sector >= report.sectors:
            report.findings.append(f"FAT sector {sector} lies beyond the end of the file")

    # -- the FAT itself ---------------------------------------------------
    fat: list[int] = []
    for sector in difat[:fat_count]:
        if sector >= report.sectors:
            continue
        offset = HEADER_SIZE + sector * sector_size
        fat.extend(struct.unpack_from(f"<{sector_size // 4}I", data, offset))
    if len(fat) < fat_count * (sector_size // 4):
        report.findings.append("the FAT is shorter than the declared FAT sector count")
    report.free_sectors = sum(1 for value in fat if value == FREESECT)

    def chain(start: int, table: list[int], limit: int) -> list[int]:
        """Sectors reachable from ``start``, including the one that terminates.

        The final sector of a chain holds payload and is marked ENDOFCHAIN; it is
        part of the chain even though following the table from it ends the walk.
        Excluding it under-counts every stream by one sector, which makes a correct
        container look short and a validator useless.
        """
        out: list[int] = []
        current = start
        while 0 <= current < len(table):
            if current == FREESECT or current == ENDOFCHAIN:
                break
            if current in out:
                report.findings.append(f"chain from {start} loops at sector {current}")
                return out
            out.append(current)
            if len(out) > limit:
                report.findings.append(f"chain from {start} exceeds {limit} sectors")
                return out
            current = table[current]
        else:
            if current not in (FREESECT, ENDOFCHAIN):
                report.findings.append(f"chain from {start} leaves the FAT at {current}")
            return out
        if current == FREESECT and out:
            report.findings.append(f"chain from {start} runs into a free sector")
        return out

    # -- directory --------------------------------------------------------
    dir_chain = chain(dir_start, fat, report.sectors)
    if not dir_chain:
        report.findings.append("the directory chain is empty")
        return report
    dir_bytes = b"".join(
        data[HEADER_SIZE + sector * sector_size : HEADER_SIZE + (sector + 1) * sector_size]
        for sector in dir_chain
    )

    entries: list[dict[str, object]] = []
    for offset in range(0, len(dir_bytes) - 127, 128):
        name_len = struct.unpack_from("<H", dir_bytes, offset + 64)[0]
        if name_len <= 2 or name_len > 128:
            continue
        entries.append(
            {
                "name": dir_bytes[offset : offset + name_len - 2].decode("utf-16-le", "replace"),
                "type": dir_bytes[offset + 66],
                "start": struct.unpack_from("<I", dir_bytes, offset + 116)[0],
                "size": struct.unpack_from("<I", dir_bytes, offset + 120)[0],
            }
        )
    if not any(entry["type"] == 5 for entry in entries):
        report.findings.append("no root directory entry")
        return report

    # -- mini FAT and the mini stream -------------------------------------
    minifat: list[int] = []
    if minifat_start not in (ENDOFCHAIN, FREESECT):
        minifat_chain = chain(minifat_start, fat, report.sectors)
        if len(minifat_chain) != minifat_count:
            report.findings.append(
                f"header declares {minifat_count} mini FAT sector(s) but the chain holds "
                f"{len(minifat_chain)}"
            )
        for sector in minifat_chain:
            offset = HEADER_SIZE + sector * sector_size
            minifat.extend(struct.unpack_from(f"<{sector_size // 4}I", data, offset))

    root = next(entry for entry in entries if entry["type"] == 5)
    root_chain = chain(int(root["start"]), fat, report.sectors) if int(root["start"]) >= 0 else []
    mini_stream = b"".join(
        data[HEADER_SIZE + sector * sector_size : HEADER_SIZE + (sector + 1) * sector_size]
        for sector in root_chain
    )
    mini_total = len(mini_stream) // mini_size

    # -- allocated space, to detect overlaps ------------------------------
    used_sectors: dict[int, str] = {}
    for sector in difat[:fat_count]:
        used_sectors[sector] = "FAT"
    for sector in dir_chain:
        used_sectors.setdefault(sector, "directory")
    for sector in root_chain:
        used_sectors.setdefault(sector, "mini stream")
    if minifat_start not in (ENDOFCHAIN, FREESECT):
        for sector in chain(minifat_start, fat, report.sectors):
            used_sectors.setdefault(sector, "mini FAT")

    def claim(sector: int, who: str) -> None:
        if sector in used_sectors and used_sectors[sector] != who:
            report.findings.append(
                f"sector {sector} is claimed by both '{used_sectors[sector]}' and '{who}'"
            )
        else:
            used_sectors[sector] = who

    # -- each stream ------------------------------------------------------
    for entry in entries:
        if entry["type"] != 2:
            continue
        name = str(entry["name"])
        size = int(entry["size"])
        start = int(entry["start"])
        report.streams[name] = size
        if size == 0:
            continue
        if size < mini_cutoff:
            if not (0 <= start < max(1, mini_total)):
                report.findings.append(
                    f"'{name}' is {size} bytes and must live in the mini stream, but its "
                    f"start index {start} is outside the {mini_total} mini sector(s) present"
                )
                continue
            sectors = chain(start, minifat, max(1, mini_total))
            capacity = len(sectors) * mini_size
            if capacity < size:
                report.findings.append(
                    f"'{name}' claims {size} bytes but its mini chain holds {capacity}"
                )
            for sector in sectors:
                claim(sector + (1 << 30), f"mini stream of '{name}'")
        else:
            if start >= report.sectors:
                report.findings.append(
                    f"'{name}' starts at sector {start}, beyond the end of the file"
                )
                continue
            sectors = chain(start, fat, report.sectors)
            capacity = len(sectors) * sector_size
            if capacity < size:
                report.findings.append(
                    f"'{name}' claims {size} bytes but its chain holds {capacity}"
                )
            for sector in sectors:
                claim(sector, f"stream '{name}'")

    # -- anything allocated but unreachable -------------------------------
    for index in range(report.sectors):
        value = fat[index] if index < len(fat) else FREESECT
        if value == FREESECT:
            continue
        if index not in used_sectors and value != DIFSECT and value != FATSECT:
            report.warnings.append(
                f"sector {index} is allocated but belongs to no chain this validator modelled"
            )
    return report


def assert_clean(report: StructuralReport, context: str = "") -> None:
    """Raise with the findings, for use in tests."""
    if report.ok:
        return
    detail = "\n  ".join(report.findings)
    raise AssertionError(f"{context}: container is not structurally sound:\n  {detail}")
