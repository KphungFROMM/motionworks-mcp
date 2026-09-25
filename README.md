# motionworks-mcp

A read-first [MCP](https://modelcontextprotocol.io) server for **Yaskawa MotionWorks IEC 3 Pro**
projects, plus the one thing a generic IEC 61131-3 checker cannot do: check Structured
Text against the project it is meant for.

## Why this exists

An offline IEC checker validates a program in isolation. It cannot know that
`MasterConv` is not a global in *this* project, or that `Y_CamIn` has no `Periodic`
input here. This server parses the project, so it can.

## Install

```sh
pip install .
# or, from a checkout without installing:
PYTHONPATH=src python -m motionworks_mcp.server
```

Standard library only. No dependencies to resolve.

## Configure

The server finds projects under the roots you give it:

```sh
# Windows
set ARYAAI_MOTIONWORKS_ROOTS=C:\Users\me\Documents\MotionWorks
# or pass roots per call to mw_list_projects, and a path per call to every other tool
```

Every tool takes paths per call, so the server holds no project state and needs no
working directory.

## Tools

**Discovery**

| Tool | Answers |
|---|---|
| `mw_ping` | liveness, version, capabilities, configured roots |
| `mw_list_projects` | which projects exist under the roots |
| `mw_describe_project` | target controller and firmware, IDE generation, counts, library list |

**Read**

| Tool | Answers |
|---|---|
| `mw_list_pous` | POU kind, language, body size, presence in the project tree |
| `mw_get_pou_decls` | declarations with `VAR`/`VAR_EXTERNAL` scope and `%` addresses |
| `mw_get_pou_body` | the body as Structured Text, or its kind and size when graphical |
| `mw_get_pou_logic` | the compiled call graph: FB instances, block types, wired operands |
| `mw_get_globals` | global declarations, the address map, and symbol comments |
| `mw_get_tasks` | task type, cycle interval, priority, watchdog |
| `mw_get_hardware` | controller, firmware, axes, EtherNet/IP instances, drive parameters |
| `mw_get_library_deps` | each declared library as **resolved**, **different revision**, or **missing** — and for a revision mismatch, the installed path it can be retargeted to |
| `mw_get_data_types` | the imported data-type list |
| `mw_search` | declarations, bodies and compiled logic across a project |

**Verify**

| Tool | Answers |
|---|---|
| `mw_validate_st` | unknown `VAR_EXTERNAL` symbols, unknown blocks, unexpected parameters, the rewrites needed for an offline checker |
| `mw_pou_impact` | callers and callees of a POU or symbol |

**Write**

| Tool | Does |
|---|---|
| `mw_plan_write` | preflight: stream roles, sector arithmetic, FAT growth, risks — reports without mutating |
| `mw_write_pou_body` | replaces a POU's Structured Text body; **staged onto a project copy by default**, returns the copy and the steps that settle acceptance |
| `mw_write_declarations` | replaces declaration text — currently reports the binary `.VGR` grid gap rather than producing a POU whose text and symbol grid disagree |

`mw_new_pou` is registered and reports `capability_disabled` with the reason: creating a
POU spans the project tree, `LIST.POU`, the POU folder, translation documents and four
internal stream names, and a partial application would leave a project the IDE cannot
open.

## Library paths move with the project, and that breaks builds

`@LIBRARY.LST` records each dependency as an **absolute path** that embeds the toolbox
revision:

```
USER;C:\Users\Public\Documents\MotionWorks IEC 3 Pro\Libraries\Cam_Toolbox_v375;LIST;0
```

Open that project on a machine whose toolbox collection installed `Cam_Toolbox_v374`, and
the IDE reports a missing library although everything the library defines is present. This
is a routine way to lose an afternoon, and it is not a defect in either project.

`mw_get_library_deps` reports the distinction — `resolved`, `different_revision`,
`missing` — and names the installed revision a stale path can be pointed at. Only the
directory is ever substituted: the third field, the library's membership name, is left
exactly as the project wrote it, and every substitution is reported as a normalisation.
A different revision can change behaviour, so it is surfaced, never assumed.

The acceptance probe applies this when it stages a copy, so a build failure in the test
means the format, not a stale path.

## Writing project files

**Staged by default.** A write lands on a disposable copy of the project (`<name>__staged-<timestamp>` beside it) and the copy is returned. The production project is untouched unless `in_place: true` is passed, and then it is backed up first under `<name>__backup/<timestamp>` — an auto-named path, never a refusal.

The writer extends the FAT properly. A 512-byte FAT sector holds 128 entries covering
65,536 bytes; a container at that size has no free entry, so *any* replacement fails under
a writer that cannot grow it. This one appends FAT sectors, links them, marks them
`FATSECT` and extends the DIFAT. Verified against the two largest POUs in the corpus,
whose bodies are past that boundary.

**`write_verification` says what is known, not what we hope.** Every write result carries a
level and the evidence behind it:

| Level | Means |
|---|---|
| `accepted` | a container written by this code was opened, **Rebuild Project**'d and **Make**'d in MotionWorks IEC 3 Pro 3.7.5.1 with no errors |
| `unverified` | this write shape has not been through the IDE; the container reads back byte-identical, and that is all that is claimed |

A write that reuses existing sectors is `accepted`. One that had to add FAT sectors is
`unverified` until that variant has been run the same way — the same code path plus FAT
allocation, but more of the container changes, so it is recorded rather than presumed.

The control that makes the `accepted` result trustworthy: a byte-identical copy of the same
project was built first and **also built clean**, with every declared library resolving. The
environment was proven before the write was added.

Neither level withholds the attempt. An unverified shape runs, and says so.

To settle a new shape, run the acceptance probe on a machine with the IDE:

```sh
python tools/acceptance_test.py --project "C:\path\to\Project"
```

It stages cosmetic edits, proves the originals are byte-identical, and prints what to open
and check.

## What the format actually is

A project is a `<name>.mwt` file beside an expanded `<name>/` directory. Almost
everything lives in the directory. Each POU is an OLE2 compound file holding four
streams with different jobs:

| Stream | Holds |
|---|---|
| `<POU>T.TXT` | worksheet comments |
| `<POU>V.VB` | the IEC declaration text |
| `<POU>V.VGR` | the variable grid; the usage word separates `VAR` from `VAR_EXTERNAL` from FB instance |
| `<POU>.STB` | the Structured Text body |
| `<POU>.GB` | a proprietary graphical (ladder) body — readable, never authored here |

`tmp.sto` beside each POU is a **build cache**. It is not source authority, and it
holds the compiled representation — `.vbc` variable tables and `.abc` network
bytecode — which is how a POU whose source was not saved is still understood.

Two layouts occur and both are handled: IDE 3.x keeps the resource at
`C/Configuration/R/Resource`, IDE 1.2.x at `C/Resource/R/Resource`.

## Two MotionWorks spellings an offline checker rejects

`mw_validate_st` rewrites these, and **reports every rewrite**:

- typed `CASE` labels — `INT#0:` becomes `0:`
- enum-typed literals — `Y_EngageMethod#Immediate` becomes `Immediate`

It also relocates `AT %` declarations out of a top-level `VAR_GLOBAL` (the checker
enforces IEC 61131-3's rule that a location belongs in a program's own `VAR`
block), returning the addresses in `meta.address_map` so nothing is lost.

When IronPLC is installed the check runs alongside, as an extra section of the same
answer. Its absence never withholds anything: `external_checker.status` becomes
`unavailable` and the symbol and block cross-checks — which are this server's own —
still run. Problems that merely name an unknown type, block or parameter
(`P2008`/`P4012`/`P4015`/`P4002`/`P4007`) mean *the vendor toolboxes are not
installed here*, not *the code is wrong*; they are counted separately.

## Response contract

Every tool returns one envelope:

```json
{
  "data": {},
  "findings": [],
  "warnings": [],
  "normalisations": [],
  "ok": true,
  "meta": {}
}
```

`findings` means the attempt ran and something is wrong with the subject matter.
`warnings` means it ran and part of the answer is missing. A tool never refuses an
attempt inside an enabled capability: a missing part of a project is a warning
beside the rest of the answer, and an unknown layout is described rather than
declined.

## Scope

Read and verify only. Nothing here writes to a project, and no tool touches a
controller. Hardware configuration and live-controller operations are out of scope
entirely.

Offline success proves file and language compatibility, **not machine safety**.
Axes, units, homing, interlocks, task rates, watchdogs, cam profiles and mechanical
clearances need their own review before anything is downloaded or moved.

## Licensing and provenance

The compound-file reader and every parser here are original code. **No Yaskawa
binary is redistributed, linked or invoked.** The vendor DLLs that ship inside a
project (`Iec61131Standard.dll`, `pcoslib.dll`, `YShMem.dll`, `YAxsGrp.dll` and the
rest) are never read as code and never shipped; only published text formats and the
user's own files are parsed, on the user's own machine.

Format knowledge was derived from projects produced by MotionWorks IEC 3 Pro
1.2.1.1 through 3.7.5.1, and cross-checked against the vendor's publicly published
MotionWorks IEC Toolbox Help (TM.MWIEC.01) and Tag Export Specification
(PN.MWIEC.01).

## Tests

```sh
PYTHONPATH=src python -m pytest tests -q
```

The suite runs against a corpus of real projects spanning three IDE generations.
Point `MOTIONWORKS_CORPUS` at a directory laid out as `<corpus>/<project>` to run it
elsewhere; without a corpus the data-dependent tests skip rather than fail.
