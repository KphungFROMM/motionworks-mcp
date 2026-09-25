"""The MotionWorks IEC project model.

A project on disk is a ``<name>.mwt`` file beside an expanded ``<name>/``
directory. Almost everything lives in the directory; the ``.mwt`` carries
project-level identity only.

Layouts differ across IDE generations, and this module handles the ones observed
rather than assuming one:

* IDE 3.x puts the resource at ``C/Configuration/R/Resource``;
* IDE 1.2.x puts it at ``C/Resource/R/Resource``;
* newer saves add sidecar streams (``project.tr2``, ``*.sn``) that this model
  neither requires nor depends on.

Nothing here refuses on an unfamiliar layout. A missing part is reported as a
warning and the rest of the answer is returned.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .cfb import CfbError, CompoundFile
from .envelope import Result

# IEC text carried in a MotionWorks payload stream, used to tell a real block of
# source from the viewer state that surrounds it.
IEC_MARKERS = ("VAR_GLOBAL", "VAR_EXTERNAL", "END_VAR", "FUNCTION_BLOCK", "PROGRAM ", "TYPE ")


def normalise_text(text: str) -> str:
    """Put MotionWorks payload text into a form an IEC parser accepts.

    MotionWorks writes CRLF into a CRLF file, so decoded payloads arrive with
    doubled carriage returns. Every caller that hands recovered text onward needs
    this, and every caller should report that it happened.
    """
    text = text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def iec_blocks(payload: bytes, min_run: int = 60) -> list[str]:
    """Recover IEC text blocks embedded in a MotionWorks payload stream.

    Two layouts occur and both are tried, because guessing from a stream name is
    unreliable:

    * ``VB`` payloads are a sequence of 4-byte little-endian length + text records,
      with binary blobs interleaved;
    * ``VGR`` payloads put viewer state first and the UTF-8 text after it.

    A candidate is kept only when it decodes cleanly and contains IEC markers, so
    binary stream content never reaches a parser as source.
    """
    out: list[str] = []

    def keep(text: str) -> None:
        if not text or not any(marker in text for marker in IEC_MARKERS):
            return
        printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
        if printable / len(text) > 0.95:
            out.append(normalise_text(text))

    index = 0
    while index + 4 <= len(payload):
        length = int.from_bytes(payload[index : index + 4], "little")
        if 0 < length <= len(payload) - index - 4:
            chunk = payload[index + 4 : index + 4 + length]
            try:
                keep(chunk.decode("utf-8"))
            except UnicodeDecodeError:
                pass
            index += 4 + length
            if index + 4 <= len(payload):
                skip = int.from_bytes(payload[index : index + 4], "little")
                if 0 < skip <= len(payload) - index - 4:
                    index += 4 + skip
            continue
        index += 1

    for match in re.finditer(rb"[\x09\x0a\x0d\x20-\x7e]{%d,}" % min_run, payload):
        try:
            keep(match.group().decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return out


@dataclass
class Pou:
    """One program organisation unit as it exists on disk."""

    name: str
    kind: str = "PROGRAM"
    directory: Path | None = None
    source_file: Path | None = None
    streams: dict[str, int] = field(default_factory=dict)

    @property
    def language(self) -> str:
        """``ST`` for a text body, ``LD`` for a graphical body, ``none`` for neither."""
        if self.directory is None:
            return "none"
        source = self.directory / "src.st1"
        if not source.is_file():
            return "none"
        try:
            container = CompoundFile(source)
        except CfbError:
            return "none"
        if container.find_suffix(".STB"):
            return "ST"
        if container.find_suffix(".GB"):
            return "LD"
        return "none"

    def body_stream(self) -> str | None:
        """Name of the stream holding this POU's body, if it has one."""
        if self.directory is None:
            return None
        source = self.directory / "src.st1"
        if not source.is_file():
            return None
        try:
            container = CompoundFile(source)
        except CfbError:
            return None
        for suffix in (".STB", ".GB"):
            entries = container.find_suffix(suffix)
            if entries:
                return entries[0].name
        return None


class Project:
    """An expanded MotionWorks IEC project directory."""

    def __init__(self, path: str | Path) -> None:
        given = Path(path)
        if given.suffix.lower() == ".mwt":
            given = given.with_suffix("")
        self.directory = given
        self.result = Result()
        self.mwt = given.with_suffix(".mwt")

        self.resource = self._locate_resource()
        self.pous: list[Pou] = []
        self._load_pous()

    # -- layout -----------------------------------------------------------

    def _locate_resource(self) -> Path | None:
        """Find the resource directory, across the layouts observed in the wild."""
        candidates = [
            self.directory / "C" / "Configuration" / "R" / "Resource",
            self.directory / "C" / "Resource" / "R" / "Resource",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        found = sorted(self.directory.glob("C/*/R/Resource")) + sorted(
            self.directory.glob("C/*/*/R/Resource")
        )
        if found:
            self.result.warn(
                "resource_layout_unexpected",
                f"resource found at an unmodelled path: {found[0].relative_to(self.directory)}",
            )
            return found[0]
        self.result.warn(
            "resource_missing",
            "no resource directory found; hardware, tasks and globals will be absent",
        )
        return None

    def _load_pous(self) -> None:
        kinds: dict[str, str] = {}
        list_pou = self.directory / "LIST.POU"
        if list_pou.is_file():
            for line in list_pou.read_text(encoding="latin-1", errors="replace").splitlines():
                parts = [part for part in line.split("\t") if part]
                if len(parts) >= 2:
                    kinds[parts[1]] = parts[0]
        else:
            self.result.warn("list_pou_missing", "LIST.POU is absent; POU kinds default to PROGRAM")

        poe = self.directory / "POE"
        if not poe.is_dir():
            self.result.warn("poe_missing", "POE directory is absent; the project has no POUs")
            return
        for directory in sorted(entry for entry in poe.iterdir() if entry.is_dir()):
            name = directory.name
            source = directory / "src.st1"
            streams: dict[str, int] = {}
            if source.is_file():
                try:
                    container = CompoundFile(source)
                    streams = {
                        entry.name: entry.size
                        for entry in container.entries
                        if entry.is_stream
                    }
                except CfbError as exc:
                    self.result.warn(
                        "pou_unreadable", f"{name}: {exc}", str(source.relative_to(self.directory))
                    )
            else:
                self.result.warn(
                    "pou_no_source", f"{name}: src.st1 is absent", str(directory.name)
                )
            self.pous.append(
                Pou(
                    name=name,
                    kind=kinds.get(name, "PROGRAM"),
                    directory=directory,
                    source_file=source if source.is_file() else None,
                    streams=streams,
                )
            )

    # -- facts ------------------------------------------------------------

    def library_list(self) -> list[dict[str, str]]:
        """Entries from ``@LIBRARY.LST``: class, path, name and version tag."""
        path = self.directory / "@LIBRARY.LST"
        if not path.is_file():
            self.result.warn("library_list_missing", "@LIBRARY.LST is absent")
            return []
        entries: list[dict[str, str]] = []
        for line in path.read_text(encoding="latin-1", errors="replace").splitlines()[1:]:
            parts = line.split(";")
            if len(parts) < 3:
                continue
            entries.append(
                {
                    "class": parts[0].strip(),
                    "path": parts[1].strip(),
                    "name": parts[2].strip(),
                    "version": parts[3].strip() if len(parts) > 3 else "",
                    "installed": str(Path(parts[1].strip()).exists()).lower(),
                }
            )
        return entries

    def _xml(self, relative: str) -> ET.Element | None:
        if self.resource is None:
            return None
        path = self.resource / relative
        if not path.is_file():
            self.result.warn(
                "config_missing", f"{relative} is absent", str(path.name)
            )
            return None
        try:
            return ET.parse(path).getroot()
        except ET.ParseError as exc:
            self.result.warn("config_unparsable", f"{relative}: {exc}", str(path.name))
            return None

    def hardware(self) -> dict[str, object]:
        """Target controller, firmware and IDE generation."""
        out: dict[str, object] = {}
        root = self._xml("ConfigFiles/hardware.xml")
        if root is not None:
            firmware = root.find("./controller/firmware")
            out["hardware_model"] = root.findtext("./model")
            out["controller"] = root.findtext("./controller/model")
            if firmware is not None:
                out["firmware_model"] = firmware.findtext("model")
                out["firmware_version"] = firmware.findtext("version")
        userdata = self._xml("ConfigFiles/userdata.xml")
        if userdata is not None:
            out["ide_version"] = userdata.findtext("./versions/ide")
            out["resource_ip"] = userdata.findtext("./ipAddress")
            out["subnet_mask"] = userdata.findtext("./subnetMask")
            out["machine_name"] = userdata.findtext("./machineName")
        return out

    def axes(self) -> list[dict[str, str]]:
        """Configured axes, in declaration order."""
        root = self._xml("ConfigFiles/axis.xml")
        if root is None:
            return []
        axes: list[dict[str, str]] = []
        for node in root.iter():
            if node.tag in ("axis", "externalAxis"):
                axes.append(
                    {
                        "kind": node.tag,
                        "name": node.findtext("name") or "",
                        "number": node.findtext("number") or "",
                    }
                )
        return axes

    def tasks(self) -> list[dict[str, str]]:
        """Task definitions parsed from the ``*.SET`` files."""
        if self.resource is None:
            return []
        tasks: list[dict[str, str]] = []
        for path in sorted(self.resource.glob("*.SET")):
            text = path.read_text(encoding="latin-1", errors="replace")
            if not text.lstrip().upper().startswith("TASK"):
                continue
            fields: dict[str, str] = {"name": path.stem}
            for key, value in re.findall(r"(\w+)\s*:=\s*([^,\n)]+)", text):
                fields[key.strip().lower()] = value.strip()
            tasks.append(fields)
        return tasks

    def io(self) -> dict[str, object]:
        """EtherNet/IP instances and the logical I/O map."""
        root = self._xml("ConfigFiles/io.xml")
        if root is None:
            return {}
        out: dict[str, object] = {"version": root.findtext("./version")}
        server = root.find("./etherNetIPIO/server")
        if server is not None:
            for key in ("t2o", "o2t"):
                node = server.find(key)
                if node is not None:
                    out[key] = {
                        "instance": node.findtext("instance"),
                        "data_size": node.findtext("dataSz"),
                    }
        manager = root.find("./ioManager")
        if manager is not None:
            out["logical_channels"] = len(list(manager))
        return out

    def global_variables(self) -> tuple[str, list[dict[str, str]]]:
        """Global IEC declarations and their direct addresses.

        Returns the declaration text plus one row per ``AT %`` variable, so a
        caller can keep the addresses as data while checking the declarations as
        code. Addresses are preserved here rather than being dropped.
        """
        if self.resource is None:
            return "", []
        source = self.resource / "src.st1"
        if not source.is_file():
            self.result.warn("globals_missing", "resource src.st1 is absent")
            return "", []
        try:
            container = CompoundFile(source)
        except CfbError as exc:
            self.result.warn("globals_unreadable", str(exc))
            return "", []
        # A payload can carry several IEC text blocks separated by binary data
        # (viewer state, icon records). Every block matters: keeping only the last
        # one silently drops whole sections of the declaration set.
        stream_name = next(
            (
                name
                for name in ("Global_Variables.VB", "GLOBAL_VARIABLES.VB")
                if container.find(name) is not None
            ),
            None,
        )
        text = ""
        if stream_name is not None:
            blocks = [
                block
                for block in iec_blocks(container.read_stream(stream_name))
                if "END_VAR" in block or "VAR_GLOBAL" in block or "VAR_EXTERNAL" in block
            ]
            text = "\n".join(blocks)
        addresses: list[dict[str, str]] = []
        for match in re.finditer(
            r"(?m)^(\s*)([A-Za-z_]\w*)\s+AT\s+(%[IQM][XBWDL]?[\d.]+)\s*:\s*([^;]+);", text
        ):
            addresses.append(
                {"name": match.group(2), "address": match.group(3), "type": match.group(4).strip()}
            )
        return text, addresses

    def variable_comments(self) -> dict[str, str]:
        """Human comments for symbols, from the translation document."""
        if self.resource is None:
            return {}
        path = self.resource / "GLOBAL_VARIABLESTranslation.xml"
        if not path.is_file():
            return {}
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            self.result.warn("translations_unparsable", str(exc))
            return {}
        comments: dict[str, str] = {}
        for item in root.findall(".//item"):
            text = (item.findtext("translation") or "").strip()
            if text:
                comments[item.get("id", "")] = text
        return comments

    def data_types(self) -> tuple[int, list[str]]:
        """Count and head of the imported data-type list."""
        for candidate in (
            self.directory / "DT" / "Tyllist.typ",
            self.resource / "ConfigFiles" / "TYLLIST.TYP" if self.resource else None,
        ):
            if candidate is None or not candidate.is_file():
                continue
            lines = [
                line
                for line in candidate.read_text(encoding="latin-1", errors="replace").splitlines()
                if line.strip()
            ]
            return len(lines), lines[:3]
        self.result.warn("data_types_missing", "no Tyllist.typ found")
        return 0, []

    def pou_names_from_tree(self) -> list[str]:
        """POU names recorded in the project tree stream.

        Used to cross-check the POU folders on disk, since a tree entry and a
        folder can disagree after an external edit.
        """
        source = self.directory / "src.st1"
        if not source.is_file():
            return []
        try:
            container = CompoundFile(source)
        except CfbError:
            return []
        if container.find("PROJECT.TRE") is None:
            return []
        tree = container.read_stream("PROJECT.TRE").decode("latin-1", "replace")
        names: list[str] = []
        for match in re.finditer(r"POE\\([^\t\\]+)\\src\.st1|POE\\([^\t\\]+)\\?[\w.]*\.(?:STB|GB)", tree):
            name = match.group(1) or match.group(2)
            if name and name not in names:
                names.append(name)
        return names
