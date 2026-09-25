"""Prove the writer on the containers the reviewed writer could not touch.

`TopCutterCutControl/src.st1` (56,832 bytes, 18 free FAT entries) and
`RotaryKnifeCamGen/src.st1` (59,904 bytes, 19 free) are the two cases where a
128-entry FAT leaves no room, so the reviewed writer raised
"CFB FAT has no room for additional sectors" for *any* replacement, including a
same-size one. Every test here runs against a throwaway copy.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from motionworks_mcp.cfb import CfbError, CompoundFile  # noqa: E402
from motionworks_mcp.cfb_write import CompoundWriter, plan_stream_replacement  # noqa: E402

CORPUS = Path(r"C:\DeepseekAI\AryaAIDSH\_scratch\yaskawa-projects\Yaskawa")
WORK = Path(r"C:\DeepseekAI\AryaAIDSH\_scratch\writer2")

CASES = [
    ("MP2600iec Program/TopCutter", "POE/TopCutterCutControl/src.st1", "TopCutterCutControl.STB"),
    ("RotaryKnife_ASP_v350", "POE/RotaryKnifeCamGen/src.st1", "RotaryKnifeCamGen.GB"),
    ("MP2600iec Program/TopCutter", "POE/TopCutterCamSetup/src.st1", "TopCutterCamSetup.STB"),
    ("MP2600iec Program/TopCutter", "POE/TopCutterInitialize/src.st1", "TopCutterInitialize.STB"),
]

# A payload large enough to push a container past its 128-entry FAT, which is
# where the reviewed writer stopped dead. This is the case that needs FAT growth.
GROWTH = 40_000


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    failures = 0

    for project, relative, stream in CASES:
        source = CORPUS / project / relative
        if not source.is_file():
            print(f"SKIP  {relative} (absent)")
            continue
        before_source = sha(source)

        print(f"\n=== {project} / {relative}")
        reader = CompoundFile(source)
        print(f"    file {len(reader.data)} bytes, FAT {reader.capacity_note()}")
        if stream is None:
            stream = "Initialize.STB"
        entry = reader.find(stream)
        if entry is None:
            candidates = reader.stream_names()
            print(f"    no '{stream}'; streams are {candidates}")
            failures += 1
            continue
        original = reader.read_stream(stream)
        print(f"    stream {stream}: {len(original)} bytes")

        for label, payload in (
            ("same size", original),
            ("+9000 bytes", original + b"X" * 9000),
            (f"+{GROWTH} bytes", original + b"Y" * GROWTH),
            ("-50 bytes", original[:-50] if len(original) > 50 else original),
        ):
            target = WORK / f"{Path(relative).parent.name}-{label.replace(' ', '_').replace('+', 'p').replace('-', 'm')}.st1"
            shutil.copy2(source, target)
            plan = plan_stream_replacement(target, {stream: payload})
            try:
                writer = CompoundWriter(target)
                writer.replace_stream(stream, payload)
                writer.save()
            except Exception as exc:  # noqa: BLE001 - the outcome is the point
                print(f"    {label:<12} FAIL {type(exc).__name__}: {exc}")
                failures += 1
                continue

            # verify: reopen, read every other stream back, compare
            reopened = CompoundFile(target)
            read_back = reopened.read_stream(stream)
            intact = True
            for name in reader.stream_names():
                if name == stream:
                    continue
                try:
                    if reopened.read_stream(name) != reader.read_stream(name):
                        intact = False
                        print(f"       !! stream '{name}' changed unexpectedly")
                except Exception as exc:  # noqa: BLE001
                    intact = False
                    print(f"       !! stream '{name}' unreadable: {exc}")
            ok = read_back == payload
            print(
                f"    {label:<12} wrote={len(payload):<7} readback={'match' if ok else 'MISMATCH'} "
                f"others_intact={intact} size={target.stat().st_size} "
                f"fat_growth={plan.fat_growth_sectors} risks={len(plan.risks)}"
            )
            if not (ok and intact):
                failures += 1

        if sha(source) != before_source:
            print("    !! the SOURCE container changed - that is a defect")
            failures += 1

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'all writer cases passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
