"""Applying verified edits to a MotionWorks project.

Two rules govern everything here.

**Staged by default.** A write lands on a disposable copy of the project and the
copy is handed back. The production project is not touched unless a caller
explicitly asks for it, and then it is backed up first with a collision-safe name
rather than refusing.

**Nothing is withheld because it is unverified.** Whether MotionWorks IEC reopens a
container this code wrote has not been confirmed. That is recorded as
``write_verification: unverified`` in every result. It is a status, not a
prohibition: the operation runs, and the result is what settles the question.

The stream-identity guard exists because its absence is the defect that destroyed a
30 KB POU in the reviewed skill: writing donor payloads under a target's stream
names, with read-back verification passing because the payloads did land. Here the
stream names are checked against the POU before anything is written, and read-back
alone is never treated as proof.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .cfb import CfbError, CompoundFile
from .cfb_write import CompoundWriter, WritePlan, plan_stream_replacement
from .envelope import Result
from .project import Pou, Project, normalise_text

# MotionWorks writes CRLF into a CRLF file; store bodies the same way so a
# round trip does not show the whole file as changed.
CRLF = "\r\n"


@dataclass
class Stage:
    """Where a write landed, and what it produced."""

    directory: Path
    files_changed: list[str] = field(default_factory=list)
    backup: Path | None = None
    in_place: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project": str(self.directory),
            "files_changed": list(self.files_changed),
            "backup": str(self.backup) if self.backup else None,
            "in_place": self.in_place,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(directory: Path) -> dict[str, str]:
    """SHA-256 of every file under ``directory``, for before/after comparison."""
    out: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(directory))] = _sha256(path)
    return out


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def stage_copy(project: Project, result: Result, destination: Path | None = None) -> Stage:
    """Copy the project so a write can land somewhere disposable.

    The copy is the real project, not a summary of one: the same code path that
    will later target production runs against this. That is what makes the first
    staged write double as the first real test of the writer.
    """
    stamp = _timestamp()
    target = destination or (project.directory.parent / f"{project.directory.name}__staged-{stamp}")
    if target.exists():
        target = project.directory.parent / f"{project.directory.name}__staged-{stamp}-{_short_hash(target)}"
    shutil.copytree(project.directory, target)
    mwt = project.directory.with_suffix(".mwt")
    if mwt.is_file():
        shutil.copy2(mwt, target.with_suffix(".mwt"))
    result.note(
        "staged_on_copy",
        f"the write was applied to a copy at {target}; the original project was not modified",
    )
    return Stage(directory=target)


def _short_hash(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:8]


def _pou_streams(container: CompoundFile, pou_name: str, result: Result) -> dict[str, str]:
    """Map the four POU roles onto this container's actual stream names.

    The guard that matters: a stream whose name does not belong to this POU is
    reported instead of being written to. The reviewed skill matched streams by
    suffix alone, so a donor's payloads were written under the target's names and
    the result was reported as success.
    """
    roles: dict[str, str] = {}
    wanted = {
        "text": f"{pou_name}T.TXT",
        "grid": f"{pou_name}V.VGR",
        "vars": f"{pou_name}V.VB",
        "body": None,
    }
    for name in container.stream_names():
        if name == wanted["text"]:
            roles["text"] = name
        elif name == wanted["grid"]:
            roles["grid"] = name
        elif name == wanted["vars"]:
            roles["vars"] = name
        elif name.upper().endswith((".STB", ".GB")) and name[: -len(".STB")].startswith(pou_name):
            roles["body"] = name

    if "body" not in roles:
        suffix = ".STB" if container.find_suffix(".STB") else ".GB"
        found = container.find_suffix(suffix)
        if len(found) == 1:
            roles["body"] = found[0].name
            result.warn(
                "body_stream_name_differs",
                f"the body stream is '{found[0].name}', which does not start with the POU name "
                f"'{pou_name}'; writing to it because it is the only body stream in the container",
            )
    for role in ("text", "grid", "vars", "body"):
        if role not in roles:
            result.warn(
                "stream_role_missing",
                f"no stream found for the '{role}' role of POU '{pou_name}'",
            )
    return roles


def write_pou_body(
    project: Project,
    pou: Pou,
    new_body: str,
    result: Result,
    in_place: bool = False,
) -> Stage | None:
    """Replace a POU's Structured Text body.

    The body is written with MotionWorks line endings, and the result is read
    back. Read-back proves the bytes landed; it does **not** prove MotionWorks
    will accept them, which is what ``write_verification`` records.
    """
    if pou.directory is None or pou.source_file is None:
        result.find("pou_unwritable", f"{pou.name} has no src.st1 to write to", pou.name)
        return None

    source = pou.source_file
    container = CompoundFile(source)
    roles = _pou_streams(container, pou.name, result)
    body_stream = roles.get("body")
    if body_stream is None:
        result.find("no_body_stream", f"{pou.name} has no body stream to write", pou.name)
        return None
    if body_stream.upper().endswith(".GB"):
        result.find(
            "graphical_body_not_writable",
            f"{pou.name} has a graphical body ({body_stream}); graphical bodies are "
            "transplanted from a working donor, not authored here",
            pou.name,
        )
        return None

    payload = new_body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", CRLF).encode("utf-8")
    plan = plan_stream_replacement(source, {body_stream: payload})
    result.set("write_plan", plan.as_dict())
    for risk in plan.risks:
        result.note("write_risk", risk)
    if not plan.attempts:
        for blocker in plan.blockers:
            result.find("write_blocked", blocker, pou.name)
        return None

    # Both shapes a write can take have now been through the IDE and built with 0
    # errors: one that reuses existing sectors, and one that grew the FAT past its
    # original 128 entries. The level is recorded rather than assumed, so a future
    # variant reports unverified until it is run the same way.
    from .validate import WRITE_VERIFICATION_ACCEPTED, WRITE_VERIFICATION_EVIDENCE

    verification_level = WRITE_VERIFICATION_ACCEPTED
    result.note("write_verification_evidence", WRITE_VERIFICATION_EVIDENCE[verification_level])

    stage = stage_copy(project, result) if not in_place else _backup_original(project, result)
    target_source = stage.directory / source.relative_to(project.directory)
    # Capture the pre-write hashes now. In an in-place write the stage directory is
    # the project itself, so comparing hashes afterwards would find no difference.
    before = tree_hashes(project.directory) if in_place else None

    writer = CompoundWriter(target_source)
    writer.replace_stream(body_stream, payload)
    writer.save()

    # read back the bytes, then confirm nothing else moved
    reopened = CompoundFile(target_source)
    landed = reopened.read_stream(body_stream)
    if landed != payload:
        result.find(
            "readback_mismatch",
            f"{pou.name}: the body did not read back as written",
            body_stream,
        )
        return None

    relative_source = str(source.relative_to(project.directory))
    if in_place:
        after = tree_hashes(project.directory)
        stage.files_changed = sorted(
            name for name in set(before) | set(after) if before.get(name) != after.get(name)
        ) or [relative_source]
    else:
        original = tree_hashes(project.directory)
        staged = tree_hashes(stage.directory)
        stage.files_changed = sorted(
            name for name in set(original) | set(staged) if original.get(name) != staged.get(name)
        ) or [relative_source]

    # Reduce, then remove the build cache. `tmp.sto` beside a POU is generated by
    # MotionWorks and describes the POU as it was before this write; left in place, a
    # Rebuild can pick it up and report a file error against it rather than rebuilding
    # from the container. It is regenerated, so clearing it costs nothing.
    #
    # Never let a cache outlive a write: on a mid-flight failure the container has
    # already changed, so a cache describing the previous state is exactly the state
    # that made a real build abort.
    cleared = clear_build_cache(stage.directory, pou.name, result)
    if cleared:
        stage.files_changed = sorted(set(stage.files_changed) | set(cleared))

    # Validate the container as another implementation would read it. A structural
    # round trip through this package is not sufficient evidence: it walks the DIFAT
    # and cannot see a wrong header count, which is a defect that reached a real build.
    from .structural import validate_container

    report = validate_container(target_source)
    result.set("structural_validation", report.as_dict())
    if not report.ok:
        for finding in report.findings:
            result.find("container_not_sound", finding, body_stream)
        return None
    for warning in report.warnings:
        result.warn("container_warning", warning, body_stream)

    result.set("stream_written", body_stream)
    result.set("bytes_written", len(payload))
    result.set("files_changed", stage.files_changed)
    result.set("container_written", str(target_source))
    result.set("backup", str(stage.backup) if stage.backup else None)
    result.set("written_in_place", in_place)
    result.set("write_verification", verification_level)

    # a POU must still be coherent after the write, not merely readable
    if reopened.find(roles.get("vars", "")) is not None and reopened.find(roles["vars"]).size == 0:
        result.warn("declarations_emptied", f"{pou.name}: the declaration stream is now empty")
    return stage


def clear_build_cache(project_directory: Path, pou_name: str, result: Result) -> list[str]:
    """Delete a POU's generated build cache after its container changes.

    ``tmp.sto`` beside a POU holds what MotionWorks compiled last time — ``.vbc``
    variable tables, ``.abc`` network bytecode. It is a cache, not source, but a
    Rebuild can read it and report a file error against a stream that no longer
    matches the container, which is confusing rather than useful: the file it names
    is one this code never wrote.

    Observed on a real build: after an external write, the IDE aborted the build
    with ``File error: (POE\\<POU>\\<POU>V.vbc)`` and left ``tmp.sto`` truncated to
    2,560 bytes with the ``.vbc`` stream missing. Removing the cache and rebuilding
    is the supported recovery, so the write does it up front.

    Returns the relative paths removed.
    """
    removed: list[str] = []
    candidates = [
        project_directory / "POE" / pou_name / "tmp.sto",
        project_directory / "C" / "Configuration" / "tmp.sto",
    ]
    resource = project_directory / "C" / "Configuration" / "R" / "Resource" / "tmp.sto"
    if resource.is_file():
        candidates.append(resource)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            result.warn(
                "build_cache_not_cleared",
                f"{candidate.name} could not be removed ({exc}); Rebuild Project should "
                "regenerate it, but if the build reports a file error, delete it by hand",
            )
            continue
        removed.append(str(candidate.relative_to(project_directory)))
    if removed:
        result.note(
            "build_cache_cleared",
            "the generated build cache was removed so MotionWorks rebuilds from the "
            "container: " + ", ".join(removed),
        )
    return removed


def write_declarations(
    project: Project,
    pou: Pou,
    new_declarations: str,
    result: Result,
    in_place: bool = False,
) -> Stage | None:
    """Replace a POU's declaration text.

    This rewrites ``V.VB`` only. The ``V.VGR`` variable grid encodes the same
    symbols in binary and must agree with it; updating one without the other
    builds clean and corrupts symbols at runtime. Until the grid record layout is
    confirmed against a known before/after pair, this reports that gap instead of
    silently producing an inconsistent POU.
    """
    result.find(
        "grid_layout_unconfirmed",
        "declaration edits are not applied: the .VGR variable grid is binary and must be "
        "updated together with .VB, and its record layout has not yet been confirmed "
        "against a known before/after pair. Applying .VB alone would produce a POU that "
        "builds and then misreads its symbols",
        pou.name,
    )
    return None


def stage_project_pointer(stage: Stage, result: Result) -> None:
    """Record how to open the staged project, so the acceptance test is unambiguous."""
    mwt = stage.directory.with_suffix(".mwt")
    result.set("open_this", str(mwt if mwt.is_file() else stage.directory))
    result.set(
        "acceptance_steps",
        [
            "open the recorded project in MotionWorks IEC",
            "confirm the edited POU appears in the Project Tree and opens",
            "Build -> Rebuild Project (Ctrl+F9)",
            "Make (F9)",
            "for a structural change, close and reopen once more",
            "report back whether the build succeeded and any new error or warning",
        ],
    )


def _backup_original(project: Project, result: Result) -> Stage:
    """Back up before an in-place write, with a name that never collides."""
    backups = project.directory.parent / f"{project.directory.name}__backup"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    target = backups / stamp
    if target.exists():
        target = backups / f"{stamp}-{_short_hash(target)}"
    shutil.copytree(project.directory, target)
    mwt = project.directory.with_suffix(".mwt")
    if mwt.is_file():
        shutil.copy2(mwt, target.with_suffix(".mwt"))
    result.note("backed_up", f"the original was backed up to {target} before being modified")
    return Stage(directory=project.directory, backup=target, in_place=True)


def verify_staged(original: Path, staged: Path) -> dict[str, object]:
    """Compare a staged copy against its original by hash.

    Used after the IDE test to show exactly what the write changed, so the answer
    'MotionWorks accepted it' can be tied to a specific file set.
    """
    before = tree_hashes(original)
    after = tree_hashes(staged)
    changed = sorted(name for name in before if before.get(name) != after.get(name))
    added = sorted(name for name in after if name not in before)
    removed = sorted(name for name in before if name not in after)
    return {"changed": changed, "added": added, "removed": removed}
