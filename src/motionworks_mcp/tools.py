"""The tool surface: schemas and handlers.

Every handler returns the envelope from :mod:`motionworks_mcp.envelope`. None of
them refuses an attempt inside an enabled capability: a missing part of a project
becomes a warning with the rest of the answer, and a genuinely unreadable path is
an exception the server turns into a tool error, not a silent empty result.

Tools take paths per call, so the server holds no project state and needs no
working directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from .cfb import CfbError, CompoundFile
from .envelope import Result
from .pou import (
    body_text,
    compiled_logic,
    declarations_text,
    parse_declarations,
    parse_variable_grid,
    declare_scope,
)
from .project import Project, iec_blocks
from .validate import validate_st

ROOTS_ENV = "ARYAAI_MOTIONWORKS_ROOTS"


# ---------------------------------------------------------------- discovery


def _roots() -> tuple[list[Path], str | None]:
    raw = os.environ.get(ROOTS_ENV, "").strip()
    if not raw:
        return [], None
    parts = [part for part in re.split(r"[;\n]", raw) if part.strip()]
    return [Path(part.strip()) for part in parts], raw


def list_projects(args: dict[str, Any]) -> Result:
    """Find MotionWorks projects under the configured roots."""
    result = Result()
    roots, raw = _roots()
    if args.get("roots"):
        roots = [Path(part) for part in args["roots"]]
        raw = ";".join(str(part) for part in roots)
    if not roots:
        result.warn(
            "no_roots_configured",
            f"no project roots configured; set {ROOTS_ENV} or pass roots explicitly",
        )
        result.set("searched_roots", [])
        result.data = []
        return result

    depth = int(args.get("depth", 4))
    found: list[dict[str, Any]] = []
    searched: list[str] = []
    for root in roots:
        searched.append(str(root))
        if not root.is_dir():
            result.warn("root_missing", f"root does not exist: {root}")
            continue
        for mwt in _walk_for_mwt(root, depth):
            expanded = mwt.with_suffix("")
            found.append(
                {
                    "name": mwt.stem,
                    "mwt": str(mwt),
                    "expanded": str(expanded),
                    "expanded_present": expanded.is_dir(),
                }
            )
    result.set("searched_roots", searched)
    result.set("root_env", raw)
    result.data = found
    return result


def _walk_for_mwt(root: Path, depth: int) -> list[Path]:
    out: list[Path] = []
    base_depth = len(root.parts)
    for current, directories, files in os.walk(root):
        if len(Path(current).parts) - base_depth >= depth:
            directories[:] = []
        for name in files:
            if name.lower().endswith(".mwt"):
                out.append(Path(current) / name)
    return sorted(out)


def ping(args: dict[str, Any]) -> Result:
    """Report liveness and configuration. Never fails."""
    result = Result()
    roots, _raw = _roots()
    result.data = {
        "server": "motionworks-mcp",
        "version": _version(),
        "capabilities": {"read": True, "validate": True, "write": False},
        "roots": [str(root) for root in roots],
        "roots_env": ROOTS_ENV,
    }
    return result


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("motionworks-mcp")
    except Exception:  # noqa: BLE001 - running from a source tree, not an install
        return "0.1.0+source"


# ------------------------------------------------------------------- project


def _project(args: dict[str, Any]) -> Project:
    raw = args.get("project")
    if not raw:
        raise ValueError("'project' is required: the .mwt file or the expanded project directory")
    path = Path(str(raw))
    if not path.exists():
        raise FileNotFoundError(f"project path does not exist: {path}")
    return Project(path)


def describe_project(args: dict[str, Any]) -> Result:
    """Summarise a project: target, generation, counts, inventory."""
    project = _project(args)
    result = project.result
    types_count, types_head = project.data_types()
    globals_text, addresses = project.global_variables()
    result.data = {
        "name": project.directory.name,
        "project_directory": str(project.directory),
        "mwt": str(project.mwt) if project.mwt.exists() else None,
        "resource_directory": str(project.resource) if project.resource else None,
        "pou_count": len(project.pous),
        "task_count": len(project.tasks()),
        "axis_count": len(project.axes()),
        "global_variable_count": len(addresses),
        "data_type_count": types_count,
        "hardware": project.hardware(),
        "io": project.io(),
        "libraries": project.library_list(),
    }
    result.set("global_declaration_chars", len(globals_text))
    result.set("data_type_head", types_head)
    return result


def list_pous(args: dict[str, Any]) -> Result:
    """List the POUs, their kind, language and sizes."""
    project = _project(args)
    result = project.result
    tree_names = set(project.pou_names_from_tree())
    rows = []
    for pou in project.pous:
        body_name = pou.body_stream()
        rows.append(
            {
                "name": pou.name,
                "kind": pou.kind,
                "language": pou.language,
                "body_stream": body_name,
                "body_size": pou.streams.get(body_name, 0) if body_name else 0,
                "declaration_size": next(
                    (size for name, size in pou.streams.items() if name.upper().endswith("V.VB")), 0
                ),
                "in_project_tree": pou.name in tree_names if tree_names else None,
            }
        )
    if tree_names:
        on_disk = {pou.name for pou in project.pous}
        for missing in sorted(tree_names - on_disk):
            result.warn(
                "tree_entry_without_folder",
                f"the project tree references '{missing}' but no POE folder holds it",
                missing,
            )
    result.data = rows
    return result


def get_pou_decls(args: dict[str, Any]) -> Result:
    """Declarations for one POU, with scopes and direct addresses."""
    project = _project(args)
    result = project.result
    name = require(args, "pou")
    pou = next((entry for entry in project.pous if entry.name == name), None)
    if pou is None:
        result.data = None
        result.warn("pou_not_found", f"no POU named '{name}'", name)
        result.set("available", [entry.name for entry in project.pous])
        return result

    text = declarations_text(pou, result)
    variables = parse_declarations(text)

    usages: list[dict[str, int]] = []
    if pou.directory is not None:
        try:
            container = CompoundFile(pou.directory / "src.st1")
            grid = container.find_suffix("V.VGR")
            if grid:
                usages = parse_variable_grid(container.read_stream(grid[0].name))
        except CfbError as exc:
            result.warn("grid_unreadable", f"{name}: {exc}", name)

    declared_count = None
    if pou.directory is not None:
        try:
            container = CompoundFile(pou.directory / "src.st1")
            grid = container.find_suffix("V.VGR")
            if grid:
                payload = container.read_stream(grid[0].name)
                if len(payload) >= 12:
                    declared_count = int.from_bytes(payload[8:12], "little")
        except CfbError:
            pass

    if declared_count is not None and len(usages) != declared_count:
        result.warn(
            "grid_count_mismatch",
            f"{name}: the variable grid declares {declared_count} record(s) but "
            f"{len(usages)} were recognised; the grid layout may differ in this version",
            name,
        )
    for index, variable in enumerate(variables[: len(usages)] if usages else []):
        if index < len(usages):
            variable.location = declare_scope(usages[index]["usage"])

    result.data = {
        "pou": pou.name,
        "kind": pou.kind,
        "language": pou.language,
        "declaration_text": text,
        "variables": [variable.as_dict() for variable in variables],
        "variable_count": len(variables),
        "grid_records": len(usages),
        "grid_declared_count": declared_count,
    }
    return result


def get_pou_body(args: dict[str, Any]) -> Result:
    """The POU body text when it is Structured Text.

    A graphical (ladder) body reports its kind and size rather than refusing; its
    compiled logic is available from get_pou_logic.
    """
    project = _project(args)
    result = project.result
    name = require(args, "pou")
    pou = next((entry for entry in project.pous if entry.name == name), None)
    if pou is None:
        result.data = None
        result.warn("pou_not_found", f"no POU named '{name}'", name)
        result.set("available", [entry.name for entry in project.pous])
        return result

    text, kind = body_text(pou, result)
    if kind == "stub":
        result.note(
            "body_not_stored",
            f"{name}: the .STB stream is present but empty - this save did not store a body. "
            "Compiled logic may still describe the POU; try get_pou_logic",
            name,
        )
    result.data = {
        "pou": pou.name,
        "kind": pou.kind,
        "language": pou.language,
        "body_kind": kind,
        "body_text": text,
        "chars": len(text),
        "lines": len([line for line in text.splitlines() if line.strip()]),
    }
    return result


def get_pou_logic(args: dict[str, Any]) -> Result:
    """The compiled call graph for one POU, from the build cache."""
    project = _project(args)
    result = project.result
    name = require(args, "pou")
    pou = next((entry for entry in project.pous if entry.name == name), None)
    if pou is None:
        result.data = None
        result.warn("pou_not_found", f"no POU named '{name}'", name)
        return result
    nodes = compiled_logic(pou, result)
    if not nodes:
        result.note(
            "no_compiled_logic",
            f"{name}: no tmp.sto build cache, or it holds no network bytecode",
            name,
        )
    result.data = {
        "pou": pou.name,
        "networks": [node.as_dict() for node in nodes],
        "network_count": len(nodes),
        "call_count": sum(len(node.calls) for node in nodes),
    }
    return result


# --------------------------------------------------------------------- facts


def get_globals(args: dict[str, Any]) -> Result:
    """Global declarations, their addresses, and their comments."""
    project = _project(args)
    result = project.result
    text, addresses = project.global_variables()
    variables = parse_declarations(text)
    result.data = {
        "declaration_text": text,
        "variables": [variable.as_dict() for variable in variables],
        "variable_count": len(variables),
        "address_map": addresses,
        "address_count": len(addresses),
        "comments": project.variable_comments() if args.get("comments", True) else {},
    }
    if not addresses:
        result.note("no_addresses", "no AT-located globals found in this project")
    return result


def get_tasks(args: dict[str, Any]) -> Result:
    """Task configuration from the resource ``*.SET`` files."""
    project = _project(args)
    result = project.result
    tasks = project.tasks()
    if not tasks:
        result.warn("no_tasks", "no TASK definitions found in the resource directory")
    result.data = tasks
    return result


def get_hardware(args: dict[str, Any]) -> Result:
    """Target, axes, I/O configuration and drive parameters."""
    project = _project(args)
    result = project.result
    out: dict[str, Any] = {
        "hardware": project.hardware(),
        "axes": project.axes(),
        "io": project.io(),
    }
    if project.resource is not None:
        drive_params = sorted(project.resource.glob("ConfigFiles/*DrivePn.xml"))
        out["drive_parameter_files"] = [path.name for path in drive_params]
        if drive_params:
            count = len(re.findall(rb"<parameter>", drive_params[0].read_bytes()))
            out["drive_parameter_count"] = count
    result.data = out
    return result


def get_library_deps(args: dict[str, Any]) -> Result:
    """Library and firmware dependencies, marked present or missing locally."""
    project = _project(args)
    result = project.result
    libraries = project.library_list()
    missing = [entry["name"] for entry in libraries if entry["installed"] == "false"]
    if missing:
        result.note(
            "libraries_not_installed",
            f"{len(missing)} declared library path(s) are not installed on this machine: "
            + ", ".join(missing[:6]),
        )
    result.data = libraries
    return result


def get_data_types(args: dict[str, Any]) -> Result:
    """The imported data-type list."""
    project = _project(args)
    result = project.result
    count, head = project.data_types()
    result.data = {"declaration_count": count, "head": head}
    return result


def search(args: dict[str, Any]) -> Result:
    """Search declarations, bodies and compiled logic across a project."""
    project = _project(args)
    result = project.result
    pattern = require(args, "query")
    limit = int(args.get("limit", 50))

    try:
        matcher = re.compile(pattern, re.I if args.get("ignore_case", True) else 0)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc

    globals_text, _addresses = project.global_variables()
    hits: list[dict[str, Any]] = []
    for number, line in enumerate(globals_text.splitlines(), start=1):
        if matcher.search(line):
            hits.append({"where": "globals", "line": number, "text": line.strip()[:300]})
            if len(hits) >= limit:
                break

    for pou in project.pous:
        if len(hits) >= limit:
            break
        text, kind = body_text(pou, result)
        if kind != "ST":
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                hits.append(
                    {"where": f"POU {pou.name} body", "line": number, "text": line.strip()[:300]}
                )
                if len(hits) >= limit:
                    break
        declarations = declarations_text(pou, result)
        for number, line in enumerate(declarations.splitlines(), start=1):
            if matcher.search(line):
                hits.append(
                    {
                        "where": f"POU {pou.name} declarations",
                        "line": number,
                        "text": line.strip()[:300],
                    }
                )
                if len(hits) >= limit:
                    break

    result.data = {"hits": hits, "hit_count": len(hits), "truncated": len(hits) >= limit}
    return result


# ------------------------------------------------------------------ validate


def validate_pou(args: dict[str, Any]) -> Result:
    """Check Structured Text against a project's own symbols and compiled usage."""
    project = _project(args)
    result = project.result
    text = args.get("st")
    pou_name = args.get("pou")
    if not text and pou_name:
        pou = next((entry for entry in project.pous if entry.name == pou_name), None)
        if pou is None:
            result.warn("pou_not_found", f"no POU named '{pou_name}'", str(pou_name))
        else:
            body, kind = body_text(pou, result)
            declarations = declarations_text(pou, result)
            text = f"{declarations}\n\n{body}" if kind == "ST" else declarations
    if not text:
        raise ValueError("provide 'st' (text to check) or 'pou' (a POU to check)")

    validate_st(
        project,
        str(text),
        result,
        location=str(pou_name or "<input>"),
        external_dialect=str(args.get("dialect", "rusty")),
    )
    return result


def pou_impact(args: dict[str, Any]) -> Result:
    """Which POUs call, and are called by, a given POU or symbol."""
    project = _project(args)
    result = project.result
    needle = require(args, "target")

    callers: list[dict[str, Any]] = []
    callees: set[str] = set()
    for pou in project.pous:
        nodes = compiled_logic(pou, result)
        for node in nodes:
            for call in node.calls:
                block = str(call.get("block", ""))
                if block == needle and pou.name != needle:
                    callers.append({"pou": pou.name, "network": node["network"], "block": block})
                if pou.name == needle and block:
                    callees.add(block)
        # a symbol reference in declarations counts as a reference
        declarations = declarations_text(pou, result)
        if needle in declarations and pou.name != needle:
            callers.append({"pou": pou.name, "network": None, "block": f"declares {needle}"})

    result.data = {
        "target": needle,
        "callers": callers,
        "callees": sorted(callees),
        "caller_count": len(callers),
    }
    if not callers and not callees:
        result.note(
            "no_references",
            f"no references to '{needle}' were found in declarations or compiled logic",
        )
    return result


def require(args: dict[str, Any], key: str) -> str:
    """Read a required string argument."""
    value = args.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"'{key}' is required")
    return str(value)


# --------------------------------------------------------------------- write


def _find_pou(project: Project, name: str) -> Any:
    """Locate a POU by name, or ``None``."""
    return next((entry for entry in project.pous if entry.name == name), None)


def plan_write(args: dict[str, Any]) -> Result:
    """Preflight a body write: what changes, and what it costs.

    Reports the sector arithmetic and the risks without mutating anything, and
    never pronounces the operation impossible. A caller sees the whole operation
    before performing it.
    """
    from .cfb_write import plan_stream_replacement
    from .write import _pou_streams

    project = _project(args)
    result = project.result
    name = require(args, "pou")
    pou = _find_pou(project, name)
    if pou is None or pou.source_file is None:
        result.data = None
        result.warn("pou_not_found", f"no writable POU named '{name}'", name)
        result.set("available", [entry.name for entry in project.pous])
        return result

    container = CompoundFile(pou.source_file)
    roles = _pou_streams(container, pou.name, result)
    body_stream = roles.get("body")
    if body_stream is None:
        result.data = None
        result.warn("no_body_stream", f"{name} has no body stream", name)
        return result

    new_body = args.get("st")
    if new_body is None:
        payload = container.read_stream(body_stream)
    else:
        payload = (
            str(new_body)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\r\n")
            .encode("utf-8")
        )
    plan = plan_stream_replacement(pou.source_file, {body_stream: payload})
    for risk in plan.risks:
        result.note("write_risk", risk)
    result.data = {
        "pou": pou.name,
        "body_stream": body_stream,
        "stream_roles": roles,
        "plan": plan.as_dict(),
        "will_target": "a staged copy of the project unless in_place is true",
    }
    result.set("write_verification", "unverified")
    return result


def write_pou_body(args: dict[str, Any]) -> Result:
    """Replace a POU's Structured Text body.

    Staged onto a disposable copy of the project by default; the copy is returned
    so it can be opened in MotionWorks and settled. ``in_place: true`` targets the
    real project after backing it up.
    """
    from .write import stage_project_pointer, write_pou_body as apply_body

    project = _project(args)
    result = project.result
    name = require(args, "pou")
    body = require(args, "st")
    in_place = bool(args.get("in_place", False))

    pou = _find_pou(project, name)
    if pou is None:
        result.data = None
        result.warn("pou_not_found", f"no POU named '{name}'", name)
        result.set("available", [entry.name for entry in project.pous])
        return result

    stage = apply_body(project, pou, body, result, in_place=in_place)
    if stage is None:
        result.data = None
        result.set("write_verification", "not_attempted")
        return result
    stage_project_pointer(stage, result)
    result.data = stage.as_dict()
    result.set("write_verification", "unverified")
    result.set(
        "note",
        "the container was written and read back byte-identical. Whether MotionWorks IEC "
        "reopens it is not confirmed until the acceptance steps below have been run",
    )
    return result


def write_declarations(args: dict[str, Any]) -> Result:
    """Replace a POU's declaration text.

    Reports the variable-grid gap rather than producing a POU whose text and
    binary symbol grid disagree.
    """
    from .write import write_declarations as apply_declarations

    project = _project(args)
    result = project.result
    name = require(args, "pou")
    declarations = require(args, "declarations")

    pou = _find_pou(project, name)
    if pou is None:
        result.data = None
        result.warn("pou_not_found", f"no POU named '{name}'", name)
        return result
    apply_declarations(project, pou, declarations, result, in_place=bool(args.get("in_place", False)))
    result.data = None
    return result


# ------------------------------------------------------------------- registry

TOOLS: dict[str, tuple[Callable[[dict[str, Any]], Result], dict[str, Any]]] = {
    "mw_ping": (
        ping,
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "description": "Report server liveness, version, capabilities and configured roots.",
        },
    ),
    "mw_list_projects": (
        list_projects,
        {
            "type": "object",
            "properties": {
                "roots": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Directories to search. Defaults to {ROOTS_ENV}.",
                },
                "depth": {"type": "integer", "description": "Maximum recursion depth. Default 4."},
            },
            "additionalProperties": False,
            "description": "Find MotionWorks projects (.mwt and expanded directory) under the roots.",
        },
    ),
    "mw_describe_project": (
        describe_project,
        {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Path to the .mwt or expanded project directory."}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "Summarise a project: target controller and firmware, IDE generation, POU/task/axis counts, library list.",
        },
    ),
    "mw_list_pous": (
        list_pous,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "List the POUs with kind, language, body size and task presence.",
        },
    ),
    "mw_get_pou_decls": (
        get_pou_decls,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "pou": {"type": "string", "description": "POU name, as listed by mw_list_pous."},
            },
            "required": ["project", "pou"],
            "additionalProperties": False,
            "description": "IEC declarations for one POU, with VAR/VAR_EXTERNAL scope and direct addresses.",
        },
    ),
    "mw_get_pou_body": (
        get_pou_body,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}, "pou": {"type": "string"}},
            "required": ["project", "pou"],
            "additionalProperties": False,
            "description": "The POU body as Structured Text, or its kind and size when graphical or not stored.",
        },
    ),
    "mw_get_pou_logic": (
        get_pou_logic,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}, "pou": {"type": "string"}},
            "required": ["project", "pou"],
            "additionalProperties": False,
            "description": "Recovered call graph from the compiled build cache: FB instances, block types and wired operands.",
        },
    ),
    "mw_get_globals": (
        get_globals,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "comments": {"type": "boolean", "description": "Include symbol comments. Default true."},
            },
            "required": ["project"],
            "additionalProperties": False,
            "description": "Global declarations, their % address map, and symbol comments.",
        },
    ),
    "mw_get_tasks": (
        get_tasks,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "Task definitions: type, cycle interval, priority and watchdog.",
        },
    ),
    "mw_get_hardware": (
        get_hardware,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "Target controller, firmware, axes, EtherNet/IP instances and drive parameters.",
        },
    ),
    "mw_get_library_deps": (
        get_library_deps,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "Library and firmware dependencies, marked present or missing on this machine.",
        },
    ),
    "mw_get_data_types": (
        get_data_types,
        {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
            "additionalProperties": False,
            "description": "The imported data-type list and how many declarations it carries.",
        },
    ),
    "mw_search": (
        search,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "query": {"type": "string", "description": "Regular expression, case-insensitive by default."},
                "limit": {"type": "integer", "description": "Maximum hits. Default 50."},
                "ignore_case": {"type": "boolean", "description": "Default true."},
            },
            "required": ["project", "query"],
            "additionalProperties": False,
            "description": "Search declarations, POU bodies and compiled logic across a project.",
        },
    ),
    "mw_validate_st": (
        validate_pou,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "st": {"type": "string", "description": "Structured Text to check."},
                "pou": {"type": "string", "description": "Or the name of a POU in the project to check."},
                "dialect": {"type": "string", "description": "Checker dialect. Default 'rusty'."},
            },
            "required": ["project"],
            "additionalProperties": False,
            "description": (
                "Check Structured Text against this project: unknown VAR_EXTERNAL symbols, "
                "unknown function blocks, unexpected block parameters, and the rewrites needed "
                "for an offline IEC checker. Findings never withhold the result."
            ),
        },
    ),
    "mw_pou_impact": (
        pou_impact,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "target": {"type": "string", "description": "POU or symbol name."},
            },
            "required": ["project", "target"],
            "additionalProperties": False,
            "description": "Callers and callees of a POU or symbol, from declarations and compiled logic.",
        },
    ),
    "mw_plan_write": (
        plan_write,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "pou": {"type": "string"},
                "st": {
                    "type": "string",
                    "description": "Proposed body. Omit to plan a no-op replacement of the current body.",
                },
            },
            "required": ["project", "pou"],
            "additionalProperties": False,
            "description": (
                "Preflight a POU body write: stream roles, sector arithmetic, FAT growth "
                "and risks. Reports without mutating, and never pronounces it impossible."
            ),
        },
    ),
    "mw_write_pou_body": (
        write_pou_body,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "pou": {"type": "string"},
                "st": {"type": "string", "description": "The new Structured Text body."},
                "in_place": {
                    "type": "boolean",
                    "description": (
                        "Write to the real project after backing it up. Default false, which "
                        "applies the write to a disposable copy and returns it."
                    ),
                },
            },
            "required": ["project", "pou", "st"],
            "additionalProperties": False,
            "description": (
                "Replace a POU's Structured Text body. Staged onto a project copy by default. "
                "Returns the copy, the files changed, and the steps that settle whether "
                "MotionWorks accepts the result."
            ),
        },
    ),
    "mw_write_declarations": (
        write_declarations,
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "pou": {"type": "string"},
                "declarations": {"type": "string", "description": "The new declaration text."},
                "in_place": {"type": "boolean"},
            },
            "required": ["project", "pou", "declarations"],
            "additionalProperties": False,
            "description": (
                "Replace a POU's declaration text. Reports the binary variable-grid gap "
                "rather than producing a POU whose text and symbol grid disagree."
            ),
        },
    ),
}

DISABLED_TOOLS: dict[str, str] = {
    "mw_new_pou": (
        "creating a POU is not implemented: it spans the project tree, LIST.POU, the POU "
        "folder, translation documents and four internal stream names, and a partial "
        "application would leave a project the IDE cannot open"
    ),
}
