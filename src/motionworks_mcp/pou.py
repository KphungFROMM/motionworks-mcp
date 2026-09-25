"""Reading one POU: declarations, body text, and compiled logic.

Three streams in a POU container carry different things, and conflating them is
the mistake this module exists to avoid:

``<POU>V.VB``
    The IEC declaration text - ``VAR`` / ``VAR_EXTERNAL`` blocks. Present even
    when the body was never written.
``<POU>V.VGR``
    The variable grid. Binary, but the usage word tells a local variable from an
    external one from a function-block instance.
``<POU>.STB`` / ``<POU>.GB``
    The body. ``.STB`` is plain IEC Structured Text. ``.GB`` is a proprietary
    graphical (ladder) body and is never authored here.

A fourth source, ``tmp.sto``, is a build cache and holds the compiled
representation: ``.vbc`` variable tables and ``.abc`` network bytecode. The
compiled logic still names every function block, its instance and its operands,
so a POU whose source is absent is not a POU whose behaviour is unknown.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .cfb import CfbError, CompoundFile
from .envelope import Result
from .project import Pou, normalise_text

# MotionWorks spellings that an offline IEC 61131-3 checker rejects. Both are
# legal CODESYS/TwinCAT usage; the checker lacks them. Rewriting is
# value-preserving and is always reported as a normalisation.
TYPE_NAMES = (
    "BOOL|SINT|USINT|BYTE|INT|UINT|WORD|DINT|UDINT|DWORD|LINT|ULINT|LWORD|"
    "REAL|LREAL|TIME|LTIME|DATE|TOD|DT|STRING|WSTRING"
)
TYPED_CASE_LABEL = re.compile(rf"(?m)^(\s*)({TYPE_NAMES})#([0-9A-Fa-f_]+)(\s*:)(?!\s*=)")
ENUM_LITERAL = re.compile(r"\b([A-Za-z_]\w*)#([A-Za-z_]\w*)")
AT_DECLARATION = re.compile(
    r"(?m)^(\s*)([A-Za-z_]\w*)\s+AT\s+(%[IQM][XBWDL]?[\d.]+)\s*:\s*([^;]+);"
)

# .VGR usage word -> what the variable is.
USAGE_LOCAL = 1
USAGE_EXTERNAL = 5
USAGE_FB_INSTANCE = 0x00040001

_VAR_BLOCK = re.compile(
    r"\b(VAR_EXTERNAL|VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR_GLOBAL|VAR)\b(.*?)\bEND_VAR\b",
    re.S | re.I,
)

_COMMENT = re.compile(r"\(\*.*?\*\)", re.S)


@dataclass
class Variable:
    """One declared symbol."""

    name: str
    type_name: str
    scope: str
    address: str | None = None
    comment: str | None = None
    instance_of: str | None = None
    location: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "type": self.type_name, "scope": self.scope}
        if self.address:
            out["address"] = self.address
        if self.comment:
            out["comment"] = self.comment
        if self.instance_of:
            out["instance_of"] = self.instance_of
        if self.location:
            out["location"] = self.location
        return out


@dataclass
class LogicNode:
    """One network of compiled logic."""

    network: int
    calls: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"network": self.network, "calls": self.calls}


def parse_declarations(text: str) -> list[Variable]:
    """Parse IEC declaration blocks into variables.

    Handles three things that appear in every real MotionWorks POU:

    * the FB-instance marker ``inst : SomeFB(*I*)``;
    * a trailing comment written *after* the semicolon, as in
      ``X AT %MX1.7.0 : BOOL;(*TRUE if running*)`` — so comments are removed
      before statements are split, not after, or each comment glues itself to the
      next declaration and that declaration is lost;
    * ``AT %`` locations on globals as well as on locals.
    """
    variables: list[Variable] = []
    for block in _VAR_BLOCK.finditer(text):
        scope = block.group(1).upper()
        body = _COMMENT.sub(" ", block.group(2))
        for raw in body.split(";"):
            statement = raw.strip()
            if not statement or ":" not in statement:
                continue
            # The location clause sits between the name and the colon:
            #   PLC_SYS_TICK_CNT  AT %MD1.0  :  DINT;
            # so it must be lifted out of the name field before the names are read,
            # or the name fails validation and the declaration is lost.
            address = None
            at = re.search(r"\bAT\s+(%[IQM][XBWDL]?[\d.]+(?:\.[\d]+)*)", statement)
            if at:
                address = at.group(1)
                statement = statement[: at.start()] + " " + statement[at.end() :]
            name_part, rest = statement.split(":", 1)
            initial = rest.find(":=")
            if initial >= 0:
                rest = rest[:initial]
            type_name = rest.strip()
            instance_of = None
            marker = re.search(r"\(\*[Ii]\*\)", type_name)
            if marker:
                instance_of = (type_name[: marker.start()] + type_name[marker.end() :]).strip()
                type_name = instance_of
            names = [part.strip() for part in name_part.split(",") if part.strip()]
            for name in names:
                if not re.match(r"^[A-Za-z_]\w*$", name):
                    continue
                variables.append(
                    Variable(
                        name=name,
                        type_name=type_name,
                        scope=scope,
                        address=address,
                        instance_of=instance_of,
                    )
                )
    return variables


def comments_by_symbol(text: str) -> dict[str, str]:
    """Map each declared symbol to the comment that follows its declaration."""
    found: dict[str, str] = {}
    for block in _VAR_BLOCK.finditer(text):
        body = block.group(2)
        for match in re.finditer(
            r"(?m)^\s*([A-Za-z_]\w*)[^\n;]*;\s*\(\*(.*?)\*\)", body, re.S
        ):
            found.setdefault(match.group(1), match.group(2).strip())
    return found


def parse_variable_grid(payload: bytes) -> list[dict[str, int]]:
    """Pull usage records out of a ``.VGR`` variable grid.

    The record prefix is six little-endian DWORDs: ``handle, usage, group,
    flags, worksheet_row, final_flags``. Only plausible records are returned, and
    the caller compares the count against the grid header rather than trusting
    either source alone.
    """
    records: list[dict[str, int]] = []
    if len(payload) < 12:
        return records
    for offset in range(12, max(12, len(payload) - 23)):
        handle, usage, group, flags, row, final = struct.unpack_from("<6I", payload, offset)
        if (
            1000 <= handle <= 10000
            and usage in (USAGE_LOCAL, USAGE_EXTERNAL, USAGE_FB_INSTANCE)
            and group == 1
            and flags == 0
            and 1 <= row <= 999
            and final == 0
        ):
            records.append({"offset": offset, "usage": usage, "handle": handle})
    return records


def declare_scope(usage: int) -> str:
    """Human name for a ``.VGR`` usage word."""
    if usage == USAGE_EXTERNAL:
        return "VAR_EXTERNAL"
    if usage == USAGE_FB_INSTANCE:
        return "FB_INSTANCE"
    return "VAR"


def normalise_for_check(text: str, result: Result, location: str) -> str:
    """Rewrite the two MotionWorks spellings a checker rejects.

    Every change is recorded as a normalisation with a count, because a silent
    rewrite of someone's program is worse than a refused check.
    """
    labels = TYPED_CASE_LABEL.findall(text)
    if labels:
        example = f"{labels[0][1]}#{labels[0][2]}: -> {labels[0][2]}:"
        result.normalised(
            "typed_case_label",
            f"{len(labels)} typed CASE label(s) rewritten, e.g. {example}",
            location,
        )
        text = TYPED_CASE_LABEL.sub(lambda m: f"{m.group(1)}{m.group(3)}{m.group(4)}", text)

    enums = [
        match
        for match in ENUM_LITERAL.findall(text)
        if match[0] not in ("16", "2", "8") and not match[0].isdigit()
    ]
    if enums:
        example = f"{enums[0][0]}#{enums[0][1]} -> {enums[0][1]}"
        result.normalised(
            "enum_literal",
            f"{len(enums)} enum-typed literal(s) rewritten, e.g. {example}",
            location,
        )
        text = ENUM_LITERAL.sub(
            lambda m: m.group(2) if not m.group(1).isdigit() else m.group(0), text
        )
    return text


def relocate_addresses(text: str, result: Result, location: str) -> tuple[str, list[dict[str, str]]]:
    """Move ``AT %`` declarations out of a top-level ``VAR_GLOBAL``.

    IEC 61131-3 permits the location in a program's own ``VAR`` block, and the
    offline checker enforces exactly that. Relocating keeps the declarations
    checkable; the addresses are returned separately so nothing is lost.
    """
    addresses = [
        {"name": match.group(2), "address": match.group(3), "type": match.group(4).strip()}
        for match in AT_DECLARATION.finditer(text)
    ]
    if addresses:
        result.normalised(
            "at_location_relocated",
            f"{len(addresses)} AT-located declaration(s) rewritten without the address; "
            "addresses returned in meta.address_map",
            location,
        )
        text = AT_DECLARATION.sub(lambda m: f"{m.group(1)}{m.group(2)} : {m.group(4)};", text)
    return text, addresses


def declarations_text(pou: Pou, result: Result) -> str:
    """The POU's own declaration text from ``V.VB``."""
    if pou.directory is None:
        return ""
    source = pou.directory / "src.st1"
    if not source.is_file():
        return ""
    try:
        container = CompoundFile(source)
    except CfbError as exc:
        result.warn("pou_unreadable", f"{pou.name}: {exc}", pou.name)
        return ""
    for name in (f"{pou.name}V.VB", f"{pou.name}V.vb"):
        if container.find(name) is not None:
            return normalise_text(container.read_stream(name).decode("utf-8", "replace")).strip()
    entries = container.find_suffix("V.VB")
    if entries:
        raw = container.read_stream(entries[0].name)
        return normalise_text(raw.decode("utf-8", "replace")).strip()
    return ""


def body_text(pou: Pou, result: Result) -> tuple[str, str]:
    """The POU body, and which kind it is.

    Returns:
        ``(text, kind)`` where kind is ``ST``, ``LD``, ``stub`` or ``none``.
        A graphical body yields ``LD`` with empty text: reading it is supported,
        authoring it is not.
    """
    if pou.directory is None:
        return "", "none"
    source = pou.directory / "src.st1"
    if not source.is_file():
        result.warn("pou_no_source", f"{pou.name}: src.st1 is absent", pou.name)
        return "", "none"
    try:
        container = CompoundFile(source)
    except CfbError as exc:
        result.warn("pou_unreadable", f"{pou.name}: {exc}", pou.name)
        return "", "none"

    stb = container.find_suffix(".STB")
    if stb:
        raw = container.read_stream(stb[0].name)
        text = normalise_text(raw.decode("utf-8", "replace")).strip()
        if not text:
            return "", "stub"
        return text, "ST"

    graphical = container.find_suffix(".GB")
    if graphical:
        result.note(
            "graphical_body",
            f"{pou.name}: ladder body ({graphical[0].size} bytes in {graphical[0].name}); "
            "readable and its compiled logic is available via get_pou_logic, "
            "but graphical bodies are not authored by this server",
            pou.name,
        )
        return "", "LD"
    return "", "none"


def compiled_logic(pou: Pou, result: Result) -> list[LogicNode]:
    """Recover the compiled call graph from ``.abc`` / ``.vbc`` build-cache streams.

    This is how a POU whose source is missing is still understood: the compiled
    form names each function-block instance, its block type, and the operands
    wired to it.
    """
    if pou.directory is None:
        return []
    sto = pou.directory / "tmp.sto"
    if not sto.is_file():
        return []
    try:
        container = CompoundFile(sto)
    except CfbError as exc:
        result.warn("logic_unreadable", f"{pou.name}: {exc}", pou.name)
        return []

    instances: dict[str, str] = {}
    for entry in container.find_suffix(".vbc"):
        if entry.size <= 0:
            continue
        text = container.read_stream(entry.name).decode("utf-8", "replace")
        for match in re.finditer(r"(?m)^@V\s+\d+\s+\d+\t(\w+)\t+:\t+([\w]+)\(\*I\*\)", text):
            instances[match.group(1)] = match.group(2)

    nodes: list[LogicNode] = []
    for entry in container.find_suffix(".abc"):
        if entry.size <= 0:
            continue
        text = container.read_stream(entry.name).decode("utf-8", "replace")
        current: LogicNode | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("@NETWORK_BEGIN"):
                current = LogicNode(network=len(nodes) + 1)
                continue
            if line.startswith("@NETWORK_END"):
                if current is not None:
                    nodes.append(current)
                    current = None
                continue
            if current is None:
                continue
            call = re.search(r"CAL\s+(\w+)(?:\(\*\s*([^*]+?)\s*\*\))?", line)
            if call:
                instance = call.group(1)
                current.calls.append(
                    {
                        "instance": instance,
                        "block": instances.get(instance, call.group(2) or "unknown"),
                        "operands": [],
                    }
                )
                continue
            operand = re.search(r"\b(LD|ST)\s+(\S+)", line)
            if operand and current.calls:
                current.calls[-1]["operands"].append(
                    {"op": operand.group(1), "operand": operand.group(2)}
                )
    return nodes


def observed_block_parameters(project_root: Path) -> dict[str, set[str]]:
    """Parameter names seen for each function block across a project.

    Built from the project's own POUs, so a parameter name that appears nowhere in
    a project is worth reporting. A name that *does* appear is not proof of
    correctness, which is why findings from this source are advisory.
    """
    observed: dict[str, set[str]] = {}
    poe = project_root / "POE"
    if not poe.is_dir():
        return observed
    for directory in sorted(entry for entry in poe.iterdir() if entry.is_dir()):
        sto = directory / "tmp.sto"
        if not sto.is_file():
            continue
        try:
            container = CompoundFile(sto)
        except CfbError:
            continue
        instances: dict[str, str] = {}
        for entry in container.find_suffix(".vbc"):
            if entry.size <= 0:
                continue
            text = container.read_stream(entry.name).decode("utf-8", "replace")
            for match in re.finditer(r"(?m)^@V\s+\d+\s+\d+\t(\w+)\t+:\t+([\w]+)\(\*I\*\)", text):
                instances[match.group(1)] = match.group(2)
        if not instances:
            continue
        for entry in container.find_suffix(".abc"):
            if entry.size <= 0:
                continue
            text = container.read_stream(entry.name).decode("utf-8", "replace")
            for match in re.finditer(r"(?m)^@BPV\s+\d+\s+\d+\t@FPNOP\t(\w+)\.(\w+)", text):
                block = instances.get(match.group(1))
                if block:
                    observed.setdefault(block, set()).add(match.group(2))
    return observed
