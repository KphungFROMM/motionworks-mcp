"""Writing OLE2 / Compound File Binary containers, including FAT growth.

The reviewed writer could not extend the FAT: with one 512-byte FAT sector there
are 128 entries covering 65,536 bytes, and a container at that size has no free
entry, so *any* stream replacement failed — including a same-size one. The two
largest POUs in the corpus are past that point, which is exactly why the write
path was unusable where it mattered.

Design note that took a rewrite to get right: FAT state is held **in memory** as
one list, and the DIFAT is derived from it. Writing entries straight into the file
while the FAT is still growing does not work, because appending a FAT sector moves
the location that the next entry must be written to — an entry in the newly added
sector cannot be written through a chain that does not yet include it.

Allocation order is still deliberate: payload sectors are appended and linked
first, then the FAT is grown to describe the final sector count, then everything is
flushed once.

Nothing here decides policy. Whether a write should happen, on which project, with
what backup, is :mod:`motionworks_mcp.write`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .cfb import ENDOFCHAIN, FATSECT, FREESECT, CfbError, CompoundFile, DirEntry

DIFAT_HEADER_SLOTS = 109


@dataclass
class WritePlan:
    """What a write would do, computed without mutating anything."""

    path: Path
    replacements: dict[str, bytes] = field(default_factory=dict)
    current_sizes: dict[str, int] = field(default_factory=dict)
    sectors_needed: int = 0
    sectors_free: int = 0
    fat_growth_sectors: int = 0
    current_size_bytes: int = 0
    projected_size_bytes: int = 0
    risks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def attempts(self) -> bool:
        """Whether the write should be attempted.

        True unless the container cannot be represented at all. A size ceiling, an
        unproven path or a missing IDE test are reported, never used to withhold
        the attempt.
        """
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "current_size_bytes": self.current_size_bytes,
            "projected_size_bytes": self.projected_size_bytes,
            "sectors_needed": self.sectors_needed,
            "sectors_free": self.sectors_free,
            "fat_growth_sectors": self.fat_growth_sectors,
            "streams": [
                {
                    "name": name,
                    "current_bytes": self.current_sizes.get(name, 0),
                    "projected_bytes": len(payload),
                }
                for name, payload in sorted(self.replacements.items())
            ],
            "risks": list(self.risks),
            "blockers": list(self.blockers),
            "attempts": self.attempts,
        }


class CompoundWriter:
    """Rewrites streams inside a copy of a CFB container.

    Everything happens in memory and lands in one :meth:`save`, so a failure part
    way through leaves no half-modified container behind.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.reader = CompoundFile(path)
        self.data = bytearray(self.reader.data)
        self.sector_size = self.reader.sector_size
        self.mini_sector_size = self.reader.mini_sector_size
        self.mini_cutoff = self.reader.mini_cutoff
        # FAT state, authoritative in memory until save()
        self.fat: list[int] = [value & 0xFFFFFFFF for value in self.reader.fat]
        self.fat_sectors: list[int] = self._read_fat_sector_chain()
        self.minifat: list[int] = [value & 0xFFFFFFFF for value in self.reader._minifat]
        self.ministream = bytearray(self.reader._ministream)
        self.ministream_dirty = False

    # -- geometry ---------------------------------------------------------

    @property
    def per_fat(self) -> int:
        """FAT entries per FAT sector."""
        return self.sector_size // 4

    def present_sectors(self) -> int:
        """Sectors currently present in the file."""
        return (len(self.data) - 512) // self.sector_size

    def _read_fat_sector_chain(self) -> list[int]:
        out: list[int] = []
        for index in range(DIFAT_HEADER_SLOTS):
            value = struct.unpack_from("<i", self.reader.data, 76 + index * 4)[0]
            if value >= 0:
                out.append(value)
        next_difat = struct.unpack_from("<i", self.reader.data, 68)[0]
        guard = 0
        while next_difat >= 0 and guard < 4096:
            offset = 512 + next_difat * self.sector_size
            per = self.sector_size // 4
            for index in range(per - 1):
                value = struct.unpack_from("<i", self.reader.data, offset + index * 4)[0]
                if value >= 0:
                    out.append(value)
            next_difat = struct.unpack_from("<i", self.reader.data, offset + (per - 1) * 4)[0]
            guard += 1
        return out

    # -- FAT in memory ----------------------------------------------------

    def _grow_fat_to(self, entries: int) -> int:
        """Ensure the FAT can address ``entries`` sectors. Returns sectors added."""
        added = 0
        while len(self.fat_sectors) * self.per_fat <= entries:
            new_sector = self.present_sectors()
            self.data.extend(b"\x00" * self.sector_size)
            while len(self.fat) <= new_sector:
                self.fat.append(FREESECT)
            if len(self.fat_sectors) >= DIFAT_HEADER_SLOTS:
                raise CfbError(
                    "the DIFAT header is full; a DIFAT sector chain is not implemented, "
                    "so this container cannot grow further"
                )
            self.fat_sectors.append(new_sector)
            self.fat[new_sector] = FATSECT
            added += 1
        return added

    def _set_fat(self, sector: int, value: int) -> None:
        while len(self.fat) <= sector:
            self.fat.append(FREESECT)
        self.fat[sector] = value & 0xFFFFFFFF

    def free_sectors(self) -> int:
        """Free FAT entries, counted over the whole in-memory FAT."""
        return sum(1 for value in self.fat if value == FREESECT)

    # -- allocation -------------------------------------------------------

    def append_sectors(self, count: int) -> list[int]:
        """Append ``count`` sectors and link them as one chain."""
        if count <= 0:
            return []
        first = self.present_sectors()
        self.data.extend(b"\x00" * (count * self.sector_size))
        indices = list(range(first, first + count))
        self._grow_fat_to(indices[-1])
        for index in indices:
            self._set_fat(index, FREESECT)
        for position, index in enumerate(indices):
            nxt = indices[position + 1] if position + 1 < len(indices) else ENDOFCHAIN
            self._set_fat(index, nxt)
        return indices

    # -- streams ----------------------------------------------------------

    def replace_stream(self, name: str, payload: bytes) -> None:
        """Replace one stream's payload, growing the container when it must.

        Raises:
            KeyError: no stream by that name. Streams are replaced, not created,
                because adding one means adding a directory entry.
        """
        entry = self.reader.find(name)
        if entry is None:
            raise KeyError(name)
        if len(payload) < self.mini_cutoff:
            self._write_small_stream(entry, payload)
        else:
            self._write_large_stream(entry, payload)

    def _write_large_stream(self, entry: DirEntry, payload: bytes) -> None:
        sectors_needed = max(1, -(-len(payload) // self.sector_size))
        existing = (
            list(self.reader.chain(entry.start_sector)) if entry.size >= self.mini_cutoff else []
        )
        reusable = existing[:sectors_needed]
        chain = list(reusable)
        if len(chain) < sectors_needed:
            chain.extend(self.append_sectors(sectors_needed - len(chain)))
        for position, sector in enumerate(chain):
            nxt = chain[position + 1] if position + 1 < len(chain) else ENDOFCHAIN
            self._set_fat(sector, nxt)
        for sector in existing[sectors_needed:]:
            self._set_fat(sector, FREESECT)

        padded = payload.ljust(sectors_needed * self.sector_size, b"\x00")
        for position, sector in enumerate(chain):
            start = position * self.sector_size
            offset = 512 + sector * self.sector_size
            self.data[offset : offset + self.sector_size] = padded[start : start + self.sector_size]
        self._set_directory_entry(entry.index, chain[0], len(payload))

    def _write_small_stream(self, entry: DirEntry, payload: bytes) -> None:
        """Write a payload that belongs in the mini stream.

        A payload that outgrows the mini-stream cutoff is promoted to its own
        normal chain instead, which is what the format expects and what the reader
        already understands.
        """
        if entry.size >= self.mini_cutoff:
            self._write_large_stream(entry, payload)
            return
        mini_sectors = max(1, -(-len(payload) // self.mini_sector_size))
        existing = self._mini_chain(entry.start_sector)
        chain = existing[:mini_sectors]
        if len(chain) < mini_sectors:
            chain.extend(self._allocate_mini_sectors(mini_sectors - len(chain)))
        for position, sector in enumerate(chain):
            nxt = chain[position + 1] if position + 1 < len(chain) else ENDOFCHAIN
            self._set_minifat(sector, nxt)
        for sector in existing[mini_sectors:]:
            self._set_minifat(sector, FREESECT)

        padded = payload.ljust(mini_sectors * self.mini_sector_size, b"\x00")
        for position, sector in enumerate(chain):
            start = position * self.mini_sector_size
            offset = sector * self.mini_sector_size
            self.ministream[offset : offset + self.mini_sector_size] = padded[
                start : start + self.mini_sector_size
            ]
        self.ministream_dirty = True
        self._set_directory_entry(entry.index, chain[0], len(payload))

    # -- mini stream ------------------------------------------------------

    def _mini_sector_total(self) -> int:
        """Mini sectors the container actually has.

        The mini stream is the root entry's own stream, so its size fixes the
        count. Deriving the total from the mini FAT's length instead over-counts,
        because the FAT sector is usually wider than the stream it describes.
        """
        root = next((item for item in self.reader.entries if item.is_root), None)
        if root is None:
            return 0
        return max(0, root.size // self.mini_sector_size)

    def _mini_chain(self, start: int) -> list[int]:
        out: list[int] = []
        sector = start
        guard = 0
        while 0 <= sector < len(self.minifat) and guard <= len(self.minifat):
            out.append(sector)
            sector = self.minifat[sector]
            guard += 1
        return out

    def _set_minifat(self, sector: int, value: int) -> None:
        while len(self.minifat) <= sector:
            self.minifat.append(FREESECT)
        self.minifat[sector] = value & 0xFFFFFFFF

    def _allocate_mini_sectors(self, count: int) -> list[int]:
        """Reuse free mini sectors, growing the root stream when there are none."""
        used: set[int] = set()
        for entry in self.reader.entries:
            if entry.is_stream and 0 < entry.size < self.mini_cutoff:
                used.update(self._mini_chain(entry.start_sector))
        free = [
            sector for sector in range(self._mini_sector_total()) if sector not in used
        ]
        if len(free) >= count:
            return free[:count]

        taken = free
        remaining = count - len(free)
        extra_root = max(1, -(-(remaining * self.mini_sector_size) // self.sector_size))
        chain = self.append_sectors(extra_root)
        root = next((item for item in self.reader.entries if item.is_root), None)
        if root is not None and root.start_sector >= 0:
            existing = list(self.reader.chain(root.start_sector))
            if existing:
                self._set_fat(existing[-1], chain[0])
        first_mini = len(self.ministream) // self.mini_sector_size
        self.ministream.extend(b"\x00" * (extra_root * self.sector_size))
        self.ministream_dirty = True
        for index in range(remaining):
            mini = first_mini + index
            self._set_minifat(mini, FREESECT)
            taken.append(mini)
        return taken

    # -- flush ------------------------------------------------------------

    def _set_directory_entry(self, index: int, start_sector: int, size: int) -> None:
        """Update a directory entry's start sector and size."""
        directory_chain = self.reader.chain(self.reader.directory_start)
        byte_index = index * 128
        position, within = divmod(byte_index, self.sector_size)
        if position >= len(directory_chain):
            raise CfbError(f"directory entry {index} lies outside the directory chain")
        offset = 512 + directory_chain[position] * self.sector_size + within
        struct.pack_into("<I", self.data, offset + 116, start_sector & 0xFFFFFFFF)
        struct.pack_into("<I", self.data, offset + 120, size & 0xFFFFFFFF)

    def _flush_fat(self) -> None:
        """Write the in-memory FAT and its DIFAT, unsigned throughout."""
        per = self.per_fat
        for position, sector in enumerate(self.fat_sectors):
            chunk = self.fat[position * per : (position + 1) * per]
            chunk = chunk + [FREESECT] * (per - len(chunk))
            offset = 512 + sector * self.sector_size
            self.data[offset : offset + self.sector_size] = struct.pack(
                f"<{per}I", *[value & 0xFFFFFFFF for value in chunk]
            )
        for index in range(DIFAT_HEADER_SLOTS):
            value = self.fat_sectors[index] if index < len(self.fat_sectors) else 0xFFFFFFFF
            struct.pack_into("<I", self.data, 76 + index * 4, value & 0xFFFFFFFF)

    def _flush_minifat(self) -> None:
        sectors = (
            self.reader.chain(self.reader.minifat_start)
            if self.reader.minifat_start >= 0
            else []
        )
        if not sectors:
            if not self.minifat:
                return
            raise CfbError("container has no mini FAT to update")
        capacity = len(sectors) * self.sector_size
        raw = struct.pack(
            f"<{len(self.minifat)}I", *[value & 0xFFFFFFFF for value in self.minifat]
        )
        if len(raw) > capacity:
            raise CfbError(
                f"the mini FAT needs {len(raw)} bytes but {capacity} are allocated; "
                "growing the mini FAT is not implemented"
            )
        padded = raw.ljust(capacity, b"\xff")
        for position, sector in enumerate(sectors):
            start = position * self.sector_size
            offset = 512 + sector * self.sector_size
            self.data[offset : offset + self.sector_size] = padded[start : start + self.sector_size]

    def _flush_ministream(self) -> None:
        if not self.ministream_dirty:
            return
        root = next((item for item in self.reader.entries if item.is_root), None)
        if root is None:
            return
        # the root chain may have been extended, so re-derive it from the FAT
        chain: list[int] = []
        sector = root.start_sector
        guard = 0
        while 0 <= sector < len(self.fat) and guard <= len(self.fat):
            chain.append(sector)
            sector = self.fat[sector]
            guard += 1
        capacity = len(chain) * self.sector_size
        payload = bytes(self.ministream)[:capacity].ljust(capacity, b"\x00")
        for position, index in enumerate(chain):
            start = position * self.sector_size
            offset = 512 + index * self.sector_size
            self.data[offset : offset + self.sector_size] = payload[start : start + self.sector_size]

    def save(self) -> None:
        """Flush every in-memory structure, then replace the file atomically."""
        self._flush_fat()
        self._flush_minifat()
        self._flush_ministream()
        temporary = self.path.with_suffix(self.path.suffix + ".new")
        temporary.write_bytes(bytes(self.data))
        temporary.replace(self.path)


def plan_stream_replacement(path: str | Path, replacements: dict[str, bytes]) -> WritePlan:
    """Compute what replacing these streams would cost, without writing.

    This answers the question the reviewed writer could not: how many sectors are
    needed and how many are free, so a caller sees the arithmetic instead of an
    exception.
    """
    reader = CompoundFile(path)
    plan = WritePlan(path=Path(path), replacements=dict(replacements))
    plan.current_size_bytes = len(reader.data)
    plan.sectors_free = reader.free_sectors()

    needed_extra = 0
    for name, payload in replacements.items():
        entry = reader.find(name)
        if entry is None:
            plan.blockers.append(f"no stream named '{name}' in this container")
            continue
        plan.current_sizes[name] = entry.size
        if len(payload) >= reader.mini_cutoff:
            sectors_needed = max(1, -(-len(payload) // reader.sector_size))
            existing = (
                len(reader.chain(entry.start_sector)) if entry.size >= reader.mini_cutoff else 0
            )
            needed_extra += max(0, sectors_needed - existing)

    plan.sectors_needed = needed_extra
    per_fat = reader.sector_size // 4
    shortfall = max(0, needed_extra - plan.sectors_free)
    plan.fat_growth_sectors = -(-shortfall // (per_fat - 1)) if shortfall else 0
    plan.projected_size_bytes = (
        plan.current_size_bytes
        + (needed_extra + plan.fat_growth_sectors) * reader.sector_size
    )
    if plan.fat_growth_sectors:
        plan.risks.append(
            f"the FAT is full: {plan.sectors_needed} sector(s) needed, "
            f"{plan.sectors_free} free, so {plan.fat_growth_sectors} FAT sector(s) will be added"
        )
    plan.risks.append(
        "container writes are unverified: whether MotionWorks IEC reopens this file "
        "has not been confirmed on this machine"
    )
    return plan
