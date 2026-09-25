"""Reading OLE2 / Compound File Binary containers.

MotionWorks IEC keeps each POU and the project configuration in a CFB container:
a directory of named streams over a FAT (and, for anything under 4096 bytes, a
mini FAT over the root entry's mini stream).

This reader is deliberately narrow: it opens, enumerates and reads streams. It
performs no writes. The write path lives in :mod:`motionworks_mcp.cfb_write` and
is exercised only on disposable copies of a project.

Format reference: [MS-CFB]. Values verified against containers produced by
MotionWorks IEC 3 Pro 1.2.1.1 through 3.7.5.1.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Directory entry object types.
TYPE_STORAGE = 1
TYPE_STREAM = 2
TYPE_ROOT = 5

# A CFB directory name is UTF-16LE, null terminated, at most 64 bytes including
# the terminator; a longer name cannot be represented.
MAX_STREAM_NAME_BYTES = 64


class CfbError(Exception):
    """The container could not be read as a compound file."""


@dataclass(frozen=True)
class DirEntry:
    """One directory entry: a stream, a storage, or the root."""

    name: str
    obj_type: int
    start_sector: int
    size: int
    index: int

    @property
    def is_stream(self) -> bool:
        return self.obj_type == TYPE_STREAM

    @property
    def is_root(self) -> bool:
        return self.obj_type == TYPE_ROOT


class CompoundFile:
    """A read-only view of a CFB container."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.data = self.path.read_bytes()
        except OSError as exc:
            raise CfbError(f"cannot read container: {exc}") from exc
        if len(self.data) < 512 or self.data[:8] != MAGIC:
            raise CfbError("not a compound file (bad signature)")
        self.sector_size = 1 << struct.unpack_from("<H", self.data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", self.data, 32)[0]
        if self.sector_size not in (512, 4096):
            raise CfbError(f"unsupported sector size: {self.sector_size}")
        self.directory_start = struct.unpack_from("<i", self.data, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", self.data, 56)[0]
        self.minifat_start = struct.unpack_from("<i", self.data, 60)[0]
        self.difat_start = struct.unpack_from("<i", self.data, 68)[0]
        self.fat = self._read_fat()
        self.entries = self._read_directory()
        self._ministream = self._read_ministream()
        self._minifat = self._read_minifat()

    # -- block plumbing ---------------------------------------------------

    def _sector(self, index: int) -> bytes:
        offset = 512 + index * self.sector_size
        return self.data[offset : offset + self.sector_size]

    def _read_fat(self) -> list[int]:
        sectors = [
            value
            for index in range(109)
            if (value := struct.unpack_from("<i", self.data, 76 + index * 4)[0]) >= 0
        ]
        next_difat = self.difat_start
        guard = 0
        while next_difat >= 0 and guard < 4096:
            block = self._sector(next_difat)
            per_sector = self.sector_size // 4
            for index in range(per_sector - 1):
                value = struct.unpack_from("<i", block, index * 4)[0]
                if value >= 0:
                    sectors.append(value)
            next_difat = struct.unpack_from("<i", block, (per_sector - 1) * 4)[0]
            guard += 1
        fat: list[int] = []
        for sector in sectors:
            fat.extend(
                struct.unpack_from(f"<{self.sector_size // 4}i", self._sector(sector), 0)
            )
        return fat

    def chain(self, start: int) -> list[int]:
        """Sector indices reachable from ``start`` through the FAT."""
        out: list[int] = []
        sector = start
        guard = 0
        while 0 <= sector < len(self.fat) and guard <= len(self.fat):
            out.append(sector)
            sector = self.fat[sector]
            guard += 1
        return out

    def _chain_bytes(self, start: int, size: int | None = None) -> bytes:
        if start < 0:
            return b""
        buf = b"".join(self._sector(sector) for sector in self.chain(start))
        return buf[:size] if size is not None else buf

    def _read_directory(self) -> list[DirEntry]:
        raw = self._chain_bytes(self.directory_start)
        entries: list[DirEntry] = []
        index = 0
        for offset in range(0, len(raw) - 127, 128):
            name_len = struct.unpack_from("<H", raw, offset + 64)[0]
            if name_len <= 2 or name_len > 128:
                continue
            entries.append(
                DirEntry(
                    name=raw[offset : offset + name_len - 2].decode("utf-16-le", "replace"),
                    obj_type=raw[offset + 66],
                    start_sector=struct.unpack_from("<i", raw, offset + 116)[0],
                    size=struct.unpack_from("<i", raw, offset + 120)[0],
                    index=index,
                )
            )
            index += 1
        return entries

    def _read_ministream(self) -> bytes:
        root = next((e for e in self.entries if e.is_root), None)
        if root is None or root.start_sector < 0:
            return b""
        return self._chain_bytes(root.start_sector)

    def _read_minifat(self) -> list[int]:
        if self.minifat_start < 0:
            return []
        raw = self._chain_bytes(self.minifat_start)
        return list(struct.unpack_from(f"<{len(raw) // 4}i", raw, 0))

    # -- public reads -----------------------------------------------------

    def stream_names(self) -> list[str]:
        """Names of every stream in the container, in directory order."""
        return [entry.name for entry in self.entries if entry.is_stream]

    def find(self, name: str) -> DirEntry | None:
        """The stream entry called ``name``, or ``None``."""
        for entry in self.entries:
            if entry.is_stream and entry.name == name:
                return entry
        return None

    def find_suffix(self, suffix: str) -> list[DirEntry]:
        """Every stream whose name ends with ``suffix``, case-insensitively."""
        upper = suffix.upper()
        return [
            entry for entry in self.entries if entry.is_stream and entry.name.upper().endswith(upper)
        ]

    def read_stream(self, name: str) -> bytes:
        """Read a stream by exact name.

        Raises:
            KeyError: no such stream.
        """
        entry = self.find(name)
        if entry is None:
            raise KeyError(name)
        return self.read_entry(entry)

    def read_entry(self, entry: DirEntry) -> bytes:
        """Read the payload of ``entry``, following mini or normal chains."""
        if entry.size <= 0:
            return b""
        if entry.size < self.mini_cutoff:
            buf = bytearray()
            sector = entry.start_sector
            guard = 0
            while 0 <= sector < len(self._minifat) and guard <= len(self._minifat):
                offset = sector * self.mini_sector_size
                buf += self._ministream[offset : offset + self.mini_sector_size]
                sector = self._minifat[sector]
                guard += 1
            return bytes(buf[: entry.size])
        return self._chain_bytes(entry.start_sector, entry.size)

    # -- diagnostics ------------------------------------------------------

    def free_sectors(self) -> int:
        """Sectors not yet allocated, i.e. headroom before the FAT is full.

        FAT entries are read as signed 32-bit, so a free sector arrives as ``-1``
        and must be widened before it is compared against :data:`FREESECT`.
        """
        return sum(1 for value in self.fat if value & 0xFFFFFFFF == FREESECT)

    def capacity_note(self) -> str:
        """A sentence describing how much room the container has left.

        Reported rather than enforced: an untested write path is not a reason to
        refuse, so callers use this to explain an outcome, not to prevent one.
        """
        total = len(self.fat)
        free = self.free_sectors()
        return (
            f"{total} FAT entries, {free} free; "
            f"addresses {total * self.sector_size // 1024} KB of sectors"
        )
