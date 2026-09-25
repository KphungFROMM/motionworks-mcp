"""Read-first MCP server for Yaskawa MotionWorks IEC 3 Pro projects.

Fourteen read and verify tools over a project's POUs, declarations, compiled
logic, tasks, axes, I/O and library dependencies, plus a Structured Text check
that joins the text against the project it is meant for.

Licensing and provenance
------------------------

The compound-file reader and every parser in this package are original code. No
Yaskawa binary is redistributed, linked or invoked. The vendor DLLs that ship
inside a MotionWorks project (``Iec61131Standard.dll``, ``pcoslib.dll``,
``YShMem.dll``, ``YAxsGrp.dll`` and the rest) are never read as code and never
shipped; only published text formats and the user's own files are parsed, on the
user's own machine.

The MotionWorks format knowledge encoded here was derived from projects produced
by MotionWorks IEC 3 Pro 1.2.1.1 through 3.7.5.1, and cross-checked against the
vendor's publicly published MotionWorks IEC Toolbox Help (TM.MWIEC.01) and Tag
Export Specification (PN.MWIEC.01).

Scope
-----

Read and verify only. Nothing in this package writes to a project, and no tool
refuses an attempt inside an enabled capability: a missing part of a project is
reported as a warning alongside the rest of the answer.
"""

from .cfb import CfbError, CompoundFile, DirEntry
from .envelope import Diagnostic, Result, disabled
from .project import Project, Pou, iec_blocks, normalise_text

__all__ = [
    "CfbError",
    "CompoundFile",
    "Diagnostic",
    "DirEntry",
    "Project",
    "Pou",
    "Result",
    "disabled",
    "iec_blocks",
    "normalise_text",
]

__version__ = "0.1.0"
