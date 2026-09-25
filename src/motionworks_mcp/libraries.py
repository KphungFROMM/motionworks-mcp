"""Resolving a project's library paths against the libraries on this machine.

``@LIBRARY.LST`` records each dependency as an absolute path:

    USER;C:\\Users\\Public\\Documents\\MotionWorks IEC 3 Pro\\Libraries\\Cam_Toolbox_v375;LIST;0

That path is machine-specific, and it embeds the toolbox revision. A project
moved to another machine — or opened against a different toolbox collection —
asks for ``Cam_Toolbox_v375`` while the machine has ``Cam_Toolbox_v374``, and the
IDE reports the library as missing even though everything it defines is present.

This module reports that mismatch and rewrites the path, which is a portability
fix rather than a change to the project's meaning: the third field, the library's
membership name, is left exactly as it was, and only the directory is retargeted
to a revision that actually exists. Substituting a different revision can change
behaviour, so it is reported and never done silently.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .envelope import Result

LIBRARY_LIST = "@LIBRARY.LST"
REVISION = re.compile(r"^(?P<stem>.+?)_v(?P<rev>\d+)(?P<suffix>[a-z]?)$", re.I)

# Where this machine keeps user libraries and firmware libraries. Both are
# per-machine, and the firmware root embeds the MotionWorks build number, so it is
# discovered rather than assumed.
USER_LIBRARY_HINTS = [r"C:\Users\Public\Documents\MotionWorks IEC 3 Pro\Libraries"]
FIRMWARE_HINTS = [r"C:\ProgramData\Yaskawa\MotionWorks IEC 3 Pro"]


@dataclass
class LibraryEntry:
    """One line of ``@LIBRARY.LST``."""

    class_name: str
    path: str
    member: str
    revision_tag: str

    @property
    def folder(self) -> str:
        return Path(self.path).name

    @property
    def resolves(self) -> bool:
        return Path(self.path).exists()

    def as_dict(self) -> dict[str, object]:
        return {
            "class": self.class_name,
            "folder": self.folder,
            "member": self.member,
            "path": self.path,
            "resolves": self.resolves,
        }


def parse_library_list(text: str) -> list[LibraryEntry]:
    """Parse an ``@LIBRARY.LST`` body, skipping its header line."""
    entries: list[LibraryEntry] = []
    for line in text.splitlines():
        if not line.strip() or line.lower().startswith("library list"):
            continue
        parts = line.split(";")
        if len(parts) < 3:
            continue
        entries.append(
            LibraryEntry(
                class_name=parts[0].strip(),
                path=parts[1].strip(),
                member=parts[2].strip(),
                revision_tag=parts[3].strip() if len(parts) > 3 else "",
            )
        )
    return entries


def installed_libraries() -> dict[str, Path]:
    """Every library folder available on this machine, keyed by folder name."""
    found: dict[str, Path] = {}
    roots: list[Path] = [Path(hint) for hint in USER_LIBRARY_HINTS]
    for hint in FIRMWARE_HINTS:
        base = Path(hint)
        if base.is_dir():
            # the build directory embeds the MotionWorks version, so glob for it
            for build in base.iterdir():
                fw_lib = build / "plc" / "FW_LIB"
                if fw_lib.is_dir():
                    roots.append(fw_lib)
    program_files = Path(r"C:\Program Files (x86)\Yaskawa\MotionWorks IEC 3 Pro\PLC")
    if program_files.is_dir():
        roots.append(program_files)
        roots.append(program_files / "eCLR")
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and entry.name not in found:
                found[entry.name] = entry
    return found


def find_compatible(folder: str, available: dict[str, Path]) -> tuple[str, Path] | None:
    """Find an installed revision of the same library, newest first.

    Only the revision suffix varies: ``Cam_Toolbox_v375`` and ``Cam_Toolbox_v374``
    are the same library at different revisions. A different stem is a different
    library and is not substituted.
    """
    if folder in available:
        return folder, available[folder]
    match = REVISION.match(folder)
    if match is None:
        return None
    stem = match.group("stem").lower()
    candidates: list[tuple[int, str, Path]] = []
    for name, path in available.items():
        other = REVISION.match(name)
        if other is None:
            continue
        if other.group("stem").lower() != stem:
            continue
        candidates.append((int(other.group("rev")), name, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, name, path = candidates[0]
    return name, path


def resolve_libraries(project_directory: Path, result: Result) -> list[dict[str, object]]:
    """Report how each declared library resolves on this machine.

    Missing paths are reported, never used to refuse: the rest of the answer is
    still returned.
    """
    path = project_directory / LIBRARY_LIST
    if not path.is_file():
        result.warn("library_list_missing", f"{LIBRARY_LIST} is absent")
        return []
    entries = parse_library_list(path.read_text(encoding="latin-1", errors="replace"))
    available = installed_libraries()
    rows: list[dict[str, object]] = []
    for entry in entries:
        row = entry.as_dict()
        row["folder"] = entry.folder
        if not entry.resolves:
            alternative = find_compatible(entry.folder, available)
            if alternative is None:
                row["status"] = "missing"
                result.warn(
                    "library_missing",
                    f"{entry.folder} is declared but not installed, and no other revision "
                    "of it is present on this machine",
                    entry.folder,
                )
            else:
                name, resolved = alternative
                row["status"] = "different_revision"
                row["compatible"] = name
                row["compatible_path"] = str(resolved)
                result.warn(
                    "library_revision_differs",
                    f"{entry.folder} is declared but not installed; {name} is, and defines "
                    "the same library. The path can be retargeted when the project is staged",
                    entry.folder,
                )
        else:
            row["status"] = "resolved"
        rows.append(row)
    return rows


def retarget_library_paths(project_directory: Path, result: Result) -> list[str]:
    """Rewrite declared library paths to installed revisions.

    Returns the substituted folder names. The membership field is preserved
    verbatim, so the project's own record of which library it uses is unchanged;
    only the directory it points at moves.
    """
    path = project_directory / LIBRARY_LIST
    if not path.is_file():
        return []
    raw = path.read_text(encoding="latin-1", errors="replace")
    entries = parse_library_list(raw)
    available = installed_libraries()
    substitutions: list[str] = []
    lines = raw.splitlines()
    rewritten: list[str] = []
    for line in lines:
        if not line.strip() or line.lower().startswith("library list"):
            rewritten.append(line)
            continue
        parts = line.split(";")
        if len(parts) < 3:
            rewritten.append(line)
            continue
        declared = Path(parts[1].strip())
        if declared.exists():
            rewritten.append(line)
            continue
        alternative = find_compatible(declared.name, available)
        if alternative is None:
            rewritten.append(line)
            continue
        name, resolved = alternative
        parts[1] = str(resolved)
        substitutions.append(f"{declared.name} -> {name}")
        rewritten.append(";".join(parts))
    if substitutions:
        # preserve the file's original line ending style
        newline = "\r\n" if "\r\n" in raw else "\n"
        body = newline.join(rewritten)
        if raw.endswith(("\n", "\r")):
            body += newline
        path.write_text(body, encoding="latin-1", newline="")
        for substitution in substitutions:
            result.normalised(
                "library_path_retargeted",
                f"{substitution} (the library's membership name is unchanged)",
                LIBRARY_LIST,
            )
    return substitutions
