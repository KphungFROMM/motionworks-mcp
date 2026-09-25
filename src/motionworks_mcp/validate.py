"""Checking Structured Text against the project it is meant for.

An offline IEC 61131-3 checker validates a program in isolation. It cannot know
that ``MasterConv`` is not a global in *this* project, or that ``Y_CamIn`` has no
``Periodic`` input here. This module supplies that join, using the project's own
declarations and its compiled logic.

Three checks, all advisory-ordered:

``unknown_external``
    A ``VAR_EXTERNAL`` name the project does not declare. This is the class of
    error that builds cleanly and fails at runtime, so it is reported as an error.
``unknown_block``
    A function block no project in the corpus instantiates. Reported as a warning:
    a new library may legitimately introduce one.
``unexpected_parameter``
    A parameter name never observed for that block. Reported as a warning, since
    the observation set comes from the project rather than from a specification.

Nothing here refuses. Findings are returned; the caller decides what they mean.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .envelope import Result
from .pou import (
    parse_declarations,
    normalise_for_check,
    relocate_addresses,
)
from .project import Project

# Block and parameter usage in a POU body: `inst(...)` and `inst.Param` / `block.Param`.
CALL_SITE = re.compile(r"(?m)^\s*([A-Za-z_]\w*)\s*\(")
NAMED_INPUT = re.compile(r"([A-Za-z_]\w*)\s*:=")
DOTTED = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

IEC_STANDARD_BLOCKS = {
    "TON", "TOF", "TP", "R_TRIG", "F_TRIG", "CTU", "CTD", "CTUD", "RS", "SR",
    "SEMA", "RTC",
}

# What is known about whether MotionWorks IEC accepts a container this code writes.
# Recorded evidence, not a policy: an unrun shape is reported as unverified rather
# than presumed, and an accepted one is reported as accepted rather than hedged.
WRITE_VERIFICATION_ACCEPTED = "accepted"
WRITE_VERIFICATION_UNVERIFIED = "unverified"

WRITE_VERIFICATION_EVIDENCE = {
    WRITE_VERIFICATION_ACCEPTED: (
        "containers written by this code have been opened, Rebuild Project'd and Make'd "
        "in MotionWorks IEC 3 Pro 3.7.5.1 with 0 errors, both with sectors reused and "
        "with the FAT grown past its original 128 entries (2026-09-25)"
    ),
    WRITE_VERIFICATION_UNVERIFIED: (
        "this write shape has not been through MotionWorks IEC; the container reads back "
        "byte-identical, but IDE acceptance is not yet observed"
    ),
}


def discover_ironplc() -> str | None:
    """Path to the IronPLC CLI, or ``None`` when it is not installed.

    Its absence never blocks validation; the external check is an extra section of
    the same answer.
    """
    override = os.environ.get("ARYAAI_IRONPLC_COMMAND") or os.environ.get("IRONPLC_COMMAND")
    if override and Path(override).exists():
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "IronPLC Compiler" / "bin" / "ironplcc.exe"
        if candidate.exists():
            return str(candidate)
    return shutil.which("ironplcc")


def declared_symbols(project: Project) -> set[str]:
    """Every symbol the project's own global declarations introduce."""
    text, _addresses = project.global_variables()
    return {variable.name for variable in parse_declarations(text)}


def validate_st(
    project: Project,
    text: str,
    result: Result,
    location: str = "<input>",
    external_dialect: str = "rusty",
) -> None:
    """Run every check and record the outcome on ``result``."""
    variables = parse_declarations(text)

    # -- what the text itself declares
    externals = [v for v in variables if v.scope == "VAR_EXTERNAL"]
    locals_in_text = [v for v in variables if v.scope != "VAR_EXTERNAL"]
    result.set("declared_locals", len(locals_in_text))
    result.set("declared_externals", len(externals))

    # -- cross-check externals against the project's globals
    project_symbols = declared_symbols(project)
    if not project_symbols:
        result.warn(
            "no_project_symbols",
            "the project declares no globals, so VAR_EXTERNAL names could not be cross-checked",
        )
    for variable in externals:
        if project_symbols and variable.name not in project_symbols:
            result.find(
                "unknown_external",
                f"VAR_EXTERNAL '{variable.name}' is not declared in this project's globals",
                variable.name,
            )

    # -- cross-check blocks and their parameters against compiled usage
    observed = observed_usage(project)
    instanced = {v.instance_of for v in variables if v.instance_of}
    for name in instanced:
        if observed and name not in observed and name not in IEC_STANDARD_BLOCKS:
            result.warn(
                "unknown_block",
                f"function block '{name}' is not instantiated anywhere in this project; "
                "check the library list if it is new",
                name,
            )
    for instance, parameter in set(DOTTED.findall(text)):
        block = next((v.instance_of for v in variables if v.name == instance), None)
        if block is None or block not in observed:
            continue
        if parameter not in observed[block] and parameter not in locals_in_text_names(locals_in_text):
            result.warn(
                "unexpected_parameter",
                f"parameter '{parameter}' has not been seen on '{block}' in this project",
                f"{instance}.{parameter}",
            )

    # -- prepare a checkable copy, reporting every rewrite
    for_check = normalise_for_check(text, result, location)
    for_check, addresses = relocate_addresses(for_check, result, location)
    if addresses:
        result.set("address_map", addresses)
    wrapped = wrap_for_check(for_check, result, location)

    # -- the external checker, when it is there
    checker = discover_ironplc()
    if checker is None:
        result.set("external_checker", {"status": "unavailable", "detail": "ironplcc not found"})
        return
    run_external_check(checker, wrapped, result, external_dialect)


TOP_LEVEL = re.compile(r"(?m)^\s*(PROGRAM|FUNCTION_BLOCK|FUNCTION|CONFIGURATION|TYPE)\b")
TOP_LEVEL_DECL = re.compile(r"(?m)^\s*VAR_GLOBAL\b")


def wrap_for_check(text: str, result: Result, location: str) -> str:
    """Give the checker a compilable unit.

    Recovered MotionWorks text is declarations plus a statement body, with no POU
    header, which an IEC parser cannot accept on its own. Top-level constructs
    already present (``PROGRAM``, ``FUNCTION_BLOCK``, ``TYPE``, ``VAR_GLOBAL``)
    are left where they are; anything else is wrapped in a program envelope so the
    external check has something to check.
    """
    if TOP_LEVEL.search(text):
        return text

    global_decls = ""
    body = text
    if TOP_LEVEL_DECL.search(text):
        blocks = list(re.finditer(r"(?ms)^\s*VAR_GLOBAL\b.*?^\s*END_VAR\b", text))
        if blocks:
            global_decls = "\n\n".join(match.group() for match in blocks)
            for match in reversed(blocks):
                body = body[: match.start()] + body[match.end() :]

    indented = "\n".join("    " + line if line.strip() else line for line in body.splitlines())
    result.normalised(
        "program_wrapper",
        "text had no POU header, so it was wrapped in a program envelope for the "
        "offline check; the wrapper is not part of the input",
        location,
    )
    return f"{global_decls}\n\nPROGRAM __mw_candidate\n{indented}\nEND_PROGRAM\n"


def locals_in_text_names(variables: list) -> set[str]:
    """Names of variables declared in the text being checked."""
    return {variable.name for variable in variables}


def observed_usage(project: Project) -> dict[str, set[str]]:
    """Parameter names observed per block across the project's compiled logic."""
    from .pou import observed_block_parameters

    return observed_block_parameters(project.directory)


def run_external_check(
    checker: str, text: str, result: Result, dialect: str
) -> None:
    """Run IronPLC over the prepared text and fold its problems into the answer."""
    import tempfile

    dialect_flag = ["--dialect", dialect]
    version = "(unknown)"
    try:
        version_run = subprocess.run(
            [checker, "version"], capture_output=True, text=True, errors="replace", timeout=30
        )
        if version_run.stdout.strip():
            version = version_run.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass

    with tempfile.TemporaryDirectory(prefix="mw-validate-") as directory:
        path = Path(directory) / "candidate.st"
        path.write_text(text, encoding="utf-8")
        try:
            run = subprocess.run(
                [checker, "check", *dialect_flag, str(path)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result.warn("external_check_failed", f"IronPLC could not be run: {exc}")
            result.set("external_checker", {"status": "failed", "detail": str(exc)})
            return

    output = re.sub(r"\x1b\[[0-9;]*m", "", (run.stdout or "") + (run.stderr or ""))
    problems = re.findall(r"error\[(\w+)\]:\s*([^\n]*)", output)
    library_codes = {"P2008", "P4012", "P4015", "P4002", "P4007"}
    unavailable = [code for code, _ in problems if code in library_codes]
    result.set(
        "external_checker",
        {
            "status": "ran",
            "tool": "ironplcc",
            "version": version,
            "dialect": dialect,
            "exit_code": run.returncode,
            "problems": [{"code": code, "message": message.strip()} for code, message in problems],
            "library_dependent_problems": len(unavailable),
            "note": (
                "the toolboxes are not installed offline, so problems that name an "
                "unknown type, block or parameter (P2008/P4012/P4015/P4002/P4007) mean "
                "'library not available here', not 'wrong code'. Syntax and semantic "
                "problems in the text itself are reported under their own codes. A full "
                "build belongs in MotionWorks with the toolboxes present."
            ),
        },
    )
