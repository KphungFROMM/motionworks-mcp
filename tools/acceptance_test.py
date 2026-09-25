"""Settle the one open question: does MotionWorks IEC accept a container we wrote?

Run this on the machine that has MotionWorks IEC installed. It stages **copies** of
a real project, writes a cosmetic edit into each, and prints exactly what to open
and what to check. Your originals are never touched.

    python tools/acceptance_test.py --project "C:\\path\\to\\Project"

Then, in MotionWorks IEC:

    1. open the printed .mwt
    2. confirm the edited POU appears in the Project Tree and opens
    3. Build -> Rebuild Project   (Ctrl+F9)
    4. Make                       (F9)
    5. close and reopen once
    6. run:  python tools/acceptance_test.py --report <staged-directory>

The edit is a comment appended to the end of the POU body. It changes no logic; if
the build is clean, the format is accepted and that is the whole answer.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from motionworks_mcp.envelope import Result  # noqa: E402
from motionworks_mcp.libraries import retarget_library_paths  # noqa: E402
from motionworks_mcp.pou import body_text  # noqa: E402
from motionworks_mcp.project import Project  # noqa: E402
from motionworks_mcp.write import (  # noqa: E402
    stage_project_pointer,
    tree_hashes,
    verify_staged,
    write_pou_body,
)

MARKER = "(* AryaAI acceptance probe - cosmetic only, no logic changed *)"

# `TopCutterCutControl` is the target that matters: at 30,405 bytes its container
# (56,832 bytes, 18 free FAT entries) is past the 64 KB ceiling a 128-entry FAT
# imposes, so writing it exercises FAT growth. It depends only on MC_* blocks and
# Y_CamIn, all of which resolve from the installed toolboxes.
#
# Deliberately NOT probed: the two cam-setup POUs. They declare CamGenerator and
# CamSegmentStruct, which come from Cam_Toolbox_v375, and that toolbox is not part
# of a standard MotionWorks IEC 3 Pro install. Including them would make the build
# fail for a reason that has nothing to do with whether the container format was
# written correctly.
TARGETS = ["TopCutterCutControl"]


def stage(project_path: Path, pou_names: list[str], out_dir: Path | None) -> int:
    project = Project(project_path)
    result = Result()
    baseline = tree_hashes(project.directory)

    # A baseline copy that differs from the project only in ways this probe would
    # have to change anyway — library paths — so a build result can be attributed.
    # Without it, "the machine lacks the toolbox revision this project names" and
    # "our write broke the container" look identical in the IDE.
    baseline_dir = project.directory.parent / f"{project.directory.name}__baseline"
    if baseline_dir.exists():
        shutil.rmtree(baseline_dir, ignore_errors=True)
        baseline_dir.with_suffix(".mwt").unlink(missing_ok=True)
    shutil.copytree(project.directory, baseline_dir)
    baseline_mwt = project.directory.with_suffix(".mwt")
    if baseline_mwt.is_file():
        shutil.copy2(baseline_mwt, baseline_dir.with_suffix(".mwt"))
    baseline_result = Result()
    baseline_subs = retarget_library_paths(baseline_dir, baseline_result)

    print(f"project      : {project.directory}")
    print(f"targets      : {', '.join(pou_names)}")
    if baseline_subs:
        print(f"library paths retargeted in BOTH copies: {', '.join(baseline_subs)}")
        print( "   (the project was saved against a toolbox revision this machine does not")
        print( "    have; only the path changes, and the library's own name is untouched)")
    print(f"\nSTEP 1 - establish the baseline (do this first)")
    print(f"   open: {baseline_dir.with_suffix('.mwt')}")
    print( "   Rebuild Project (Ctrl+F9), then Make (F9)")
    print( "   this copy is the project with only those library paths changed; if IT fails")
    print( "   to build, note the error and stop - the test cannot attribute a failure")
    print(f"\nSTEP 2 - the staged write\n")

    staged_dirs: list[Path] = []
    for name in pou_names:
        pou = next((entry for entry in project.pous if entry.name == name), None)
        if pou is None:
            print(f"  {name:<26} SKIP  (no such POU)")
            continue
        body, kind = body_text(pou, Result())
        if kind != "ST":
            print(f"  {name:<26} SKIP  (body kind is {kind}, not writable ST)")
            continue
        copy_result = Result()
        stage_obj = write_pou_body(
            project, pou, body.rstrip() + "\r\n" + MARKER + "\r\n", copy_result
        )
        if stage_obj is None:
            codes = [finding.code for finding in copy_result.findings]
            print(f"  {name:<26} FAIL  {codes}")
            continue
        stage_project_pointer(stage_obj, copy_result)
        staged_dirs.append(stage_obj.directory)

        # A staged copy is the right place to fix machine-specific library paths:
        # this project was saved against Cam_Toolbox_v375 and this machine has
        # v374. Left alone, the IDE would report a missing library and the build
        # result would say nothing about whether our container write is good.
        library_result = Result()
        substitutions = retarget_library_paths(stage_obj.directory, library_result)
        if substitutions:
            print(f"      library paths retargeted: {', '.join(substitutions)}")

        plan = copy_result.meta.get("write_plan", {})
        stream = (plan.get("streams") or [{}])[0]
        print(
            f"  {name:<26} staged  body={stream.get('projected_bytes', '?')} bytes  "
            f"container={plan.get('projected_size_bytes', '?')} bytes  "
            f"fat_growth={plan.get('fat_growth_sectors', '?')}"
        )
        print(f"      open: {copy_result.meta.get('open_this')}")

    after = tree_hashes(project.directory)
    if baseline != after:
        print("\n!! THE ORIGINAL PROJECT CHANGED - that is a defect, do not proceed")
        return 2
    print("\noriginals verified byte-identical (nothing in the source project changed)")

    print("\n--- STEP 3: report the result ---")
    print("Open each staged .mwt above, Rebuild Project, Make, close and reopen.")
    print("Then compare against STEP 1:")
    print("  baseline built clean AND staged built clean  -> the format is accepted")
    print("  baseline built clean AND staged failed       -> our write is the cause; send the error")
    print("  baseline failed too                          -> inconclusive; the error names the reason")
    for directory in staged_dirs:
        print(f"\n  python tools/acceptance_test.py --report \"{directory}\" --original \"{project.directory}\"")
    return 0


def report(original: Path, staged: Path) -> int:
    if not staged.is_dir():
        print(f"staged directory not found: {staged}")
        return 1
    diff = verify_staged(original, staged)
    print(f"original : {original}")
    print(f"staged   : {staged}")
    for label in ("changed", "added", "removed"):
        names = diff[label]
        print(f"{label:<9}: {len(names)}")
        for name in names:
            print(f"    {name}")
    changed = diff["changed"]
    if changed == ["POE\\TopCutterCamSetup\\src.st1"] or len(changed) == 1:
        print("\nexactly one container changed, which is what the write intended.")
    elif changed:
        print("\nmore than the intended container changed - investigate before trusting this")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MotionWorks IEC write-back acceptance probe")
    parser.add_argument("--project", help="Path to a MotionWorks project directory or .mwt")
    parser.add_argument("--pous", nargs="*", default=TARGETS, help="POUs to probe")
    parser.add_argument("--out", help="Unused; staged copies go beside the project")
    parser.add_argument("--report", help="Report a previously staged directory against --original")
    parser.add_argument("--original", help="The source project the staged copy came from")
    args = parser.parse_args()

    if args.report:
        if not args.original:
            print("--report needs --original <source project directory>")
            return 1
        return report(Path(args.original), Path(args.report))
    if not args.project:
        parser.print_help()
        return 1
    return stage(Path(args.project), args.pous, Path(args.out) if args.out else None)


if __name__ == "__main__":
    raise SystemExit(main())
