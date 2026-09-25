"""The MCP surface: protocol behaviour, tool contract, and the anti-refusal rules.

The plan commits to a specific contract: no tool refuses an attempt inside an
enabled capability. These tests assert that directly, including the negative case
the plan requires — a broken project must produce a partial answer with a
warning, never an exception and never an outright refusal.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from motionworks_mcp.envelope import Result, disabled
from motionworks_mcp.server import StdioServer
from motionworks_mcp.tools import DISABLED_TOOLS, TOOLS


@pytest.fixture
def server() -> StdioServer:
    return StdioServer(TOOLS, disabled=DISABLED_TOOLS)


def call(server: StdioServer, name: str, arguments: dict) -> dict:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert response is not None
    return response["result"]["structuredContent"]


# ------------------------------------------------------------------ protocol


def test_initialize_echoes_the_protocol_version(server: StdioServer) -> None:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "motionworks-mcp"


def test_notifications_get_no_reply(server: StdioServer) -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_protocol_error(server: StdioServer) -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "nonsense"})
    assert response["error"]["code"] == -32601


def test_unknown_tool_lists_what_is_available(server: StdioServer) -> None:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert response["error"]["code"] == -32601
    assert "mw_ping" in response["error"]["message"]


def test_tools_list_exposes_schemas(server: StdioServer) -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = response["result"]["tools"]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_a_tool_exception_becomes_a_tool_error_not_a_crash(server: StdioServer) -> None:
    """A missing path is a real precondition; it must be reported, not thrown."""
    payload = call(server, "mw_describe_project", {"project": r"C:\definitely\not\here"})
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "tool_error"
    assert "does not exist" in payload["findings"][0]["message"]


def test_malformed_lines_do_not_end_the_loop(server: StdioServer) -> None:
    stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
    stdout = io.StringIO()
    assert server.serve(stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}


# --------------------------------------------------------------- no refusals


def test_a_disabled_capability_is_reported_not_refused(server: StdioServer) -> None:
    """A capability that is not implemented says so; it does not refuse a call."""
    payload = call(server, "mw_new_pou", {})
    assert payload["ok"] is True
    assert payload["meta"]["capability_disabled"] is True
    assert payload["meta"]["detail"]


def test_write_tools_are_registered(server: StdioServer) -> None:
    """The write surface exists; whether acceptance is confirmed is separate."""
    for name in ("mw_plan_write", "mw_write_pou_body", "mw_write_declarations"):
        assert name in TOOLS, name


def test_declaration_edits_report_the_grid_gap(server: StdioServer, topcutter: Path) -> None:
    """Writing .VB alone would desynchronise the binary symbol grid, so it is refused."""
    payload = call(
        server,
        "mw_write_declarations",
        {"project": str(topcutter), "pou": "TopCutterCamSetup", "declarations": "VAR\nEND_VAR\n"},
    )
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "grid_layout_unconfirmed"


def test_plan_write_reports_arithmetic_without_mutating(server: StdioServer, topcutter: Path) -> None:
    before = {
        path: path.stat().st_mtime for path in topcutter.rglob("*") if path.is_file()
    }
    payload = call(server, "mw_plan_write", {"project": str(topcutter), "pou": "TopCutterCamSetup"})
    after = {path: path.stat().st_mtime for path in topcutter.rglob("*") if path.is_file()}
    assert payload["ok"] is True
    assert payload["data"]["plan"]["sectors_free"] >= 0
    assert payload["data"]["body_stream"].endswith(".STB")
    assert before == after, "preflight must not touch the project"


def test_a_staged_body_write_leaves_the_project_alone(server: StdioServer, topcutter: Path) -> None:
    """The whole point of staging: the real project is provably unchanged."""
    import hashlib

    source = topcutter / "POE" / "TopCutterCamSetup" / "src.st1"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    payload = call(
        server,
        "mw_write_pou_body",
        {
            "project": str(topcutter),
            "pou": "TopCutterCamSetup",
            "st": "(* staged *)\r\nxSelect := FALSE;\r\n",
        },
    )
    assert payload["ok"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    staged = Path(payload["data"]["project"])
    assert staged.is_dir()
    assert staged != topcutter
    # the marker landed in the copy
    from motionworks_mcp.envelope import Result
    from motionworks_mcp.pou import body_text
    from motionworks_mcp.project import Project

    copy = Project(staged)
    pou = next(entry for entry in copy.pous if entry.name == "TopCutterCamSetup")
    text, kind = body_text(pou, Result())
    assert kind == "ST"
    assert "staged" in text
    # and the other POUs in the copy are still readable
    assert any(entry.name == "TopCutterCutControl" for entry in copy.pous)
    shutil.rmtree(staged, ignore_errors=True)
    staged.with_suffix(".mwt").unlink(missing_ok=True)


def test_write_records_its_evidence_level(server: StdioServer, topcutter: Path) -> None:
    """Every write states what is known about IDE acceptance for that shape.

    A write that reuses sectors has been through the IDE; one that grew the FAT has
    not. Both report a level and the evidence behind it, and neither withholds the
    attempt.
    """
    payload = call(
        server,
        "mw_write_pou_body",
        {"project": str(topcutter), "pou": "TopCutterCamSetup", "st": "(* x *)\r\n"},
    )
    assert payload["meta"]["write_verification"] in {"accepted", "unverified"}
    evidence = [
        warning for warning in payload["warnings"] if warning["code"] == "write_verification_evidence"
    ]
    assert evidence, "a write must state the evidence behind its verification level"
    assert any("Rebuild" in step for step in payload["meta"]["acceptance_steps"])
    assert payload["meta"]["open_this"]
    staged = Path(payload["data"]["project"])
    shutil.rmtree(staged, ignore_errors=True)
    staged.with_suffix(".mwt").unlink(missing_ok=True)


def test_a_write_that_reuses_sectors_records_acceptance(server: StdioServer, topcutter: Path) -> None:
    """The observed, IDE-accepted shape is not hedged as unverified."""
    payload = call(
        server,
        "mw_write_pou_body",
        {"project": str(topcutter), "pou": "TopCutterCamSetup", "st": "(* small *)\r\n"},
    )
    assert payload["meta"]["write_plan"]["fat_growth_sectors"] == 0
    assert payload["meta"]["write_verification"] == "accepted"
    staged = Path(payload["data"]["project"])
    shutil.rmtree(staged, ignore_errors=True)
    staged.with_suffix(".mwt").unlink(missing_ok=True)


def test_a_write_that_grows_the_fat_is_not_presumed(server: StdioServer, topcutter: Path) -> None:
    """An unrun shape is reported as unverified rather than presumed to work."""
    padding = "\r\n".join(f"(* pad {index:04d} *)" for index in range(9000))
    payload = call(
        server,
        "mw_write_pou_body",
        {"project": str(topcutter), "pou": "TopCutterCutControl", "st": f"(* big *)\r\n{padding}\r\n"},
    )
    assert payload["meta"]["write_plan"]["fat_growth_sectors"] > 0
    assert payload["meta"]["write_verification"] == "unverified"
    assert payload["ok"] is True, "an unverified shape is still attempted"
    staged = Path(payload["data"]["project"])
    shutil.rmtree(staged, ignore_errors=True)
    staged.with_suffix(".mwt").unlink(missing_ok=True)


def test_missing_roots_yield_an_empty_list_and_a_warning(server: StdioServer, monkeypatch) -> None:
    monkeypatch.delenv("ARYAAI_MOTIONWORKS_ROOTS", raising=False)
    payload = call(server, "mw_list_projects", {})
    assert payload["data"] == []
    assert payload["warnings"][0]["code"] == "no_roots_configured"


def test_a_project_with_nothing_recognisable_still_answers(server: StdioServer, tmp_path: Path) -> None:
    """The anti-refusal rule: an unfamiliar layout is described, not declined."""
    empty = tmp_path / "Empty"
    empty.mkdir()
    payload = call(server, "mw_describe_project", {"project": str(empty)})
    assert payload["ok"] is True
    assert payload["data"]["pou_count"] == 0
    codes = {warning["code"] for warning in payload["warnings"]}
    assert {"resource_missing", "poe_missing"} <= codes


def test_an_unknown_pou_reports_the_available_names(server: StdioServer, topcutter: Path) -> None:
    payload = call(server, "mw_get_pou_body", {"project": str(topcutter), "pou": "NoSuchPou"})
    assert payload["data"] is None
    assert payload["warnings"][0]["code"] == "pou_not_found"
    assert payload["meta"]["available"]


def test_an_invalid_regex_is_a_real_precondition(server: StdioServer, topcutter: Path) -> None:
    payload = call(server, "mw_search", {"project": str(topcutter), "query": "(["})
    assert payload["ok"] is False
    assert "regular expression" in payload["findings"][0]["message"]


def test_envelope_is_one_shape_for_every_tool(server: StdioServer) -> None:
    for name in TOOLS:
        payload = call(server, name, {"project": r"C:\definitely\not\here"} if name != "mw_ping" else {})
        for key in ("data", "findings", "warnings", "normalisations", "ok", "meta"):
            assert key in payload, f"{name} is missing '{key}'"


def test_disabled_helper_shape() -> None:
    payload = disabled("mw_write_st", "--enable-writes", "reason")
    assert payload["ok"] is True
    assert payload["meta"]["capability"] == "mw_write_st"
    assert payload["warnings"] == []


# ---------------------------------------------------------------- real data


def test_pou_body_is_recovered_in_full(server: StdioServer, topcutter: Path) -> None:
    payload = call(server, "mw_get_pou_body", {"project": str(topcutter), "pou": "TopCutterCutControl"})
    assert payload["data"]["body_kind"] == "ST"
    assert payload["data"]["chars"] > 29000
    assert payload["data"]["lines"] > 900


def test_validation_reports_no_false_positives(server: StdioServer, topcutter: Path) -> None:
    """Every VAR_EXTERNAL in this POU is a real global, so nothing may be flagged."""
    payload = call(server, "mw_validate_st", {"project": str(topcutter), "pou": "TopCutterCutControl"})
    assert payload["ok"] is True
    assert [finding for finding in payload["findings"] if finding["code"] == "unknown_external"] == []


def test_validation_reports_the_rewrites_it_made(server: StdioServer, topcutter: Path) -> None:
    payload = call(server, "mw_validate_st", {"project": str(topcutter), "pou": "TopCutterCutControl"})
    codes = {item["code"] for item in payload["normalisations"]}
    assert {"typed_case_label", "enum_literal", "program_wrapper"} <= codes


def test_validation_catches_an_invented_external(server: StdioServer, topcutter: Path) -> None:
    text = "VAR_EXTERNAL\n  NoSuchSymbolAnywhere : BOOL;\nEND_VAR\n"
    payload = call(server, "mw_validate_st", {"project": str(topcutter), "st": text})
    assert payload["ok"] is False
    findings = [finding for finding in payload["findings"] if finding["code"] == "unknown_external"]
    assert findings and findings[0]["location"] == "NoSuchSymbolAnywhere"


def test_validation_keeps_addresses_as_data(server: StdioServer, topcutter: Path) -> None:
    text = "VAR_GLOBAL\n  X AT %IX0.0 : BOOL;\nEND_VAR\n"
    payload = call(server, "mw_validate_st", {"project": str(topcutter), "st": text})
    assert payload["meta"]["address_map"][0]["address"] == "%IX0.0"
    codes = {item["code"] for item in payload["normalisations"]}
    assert "at_location_relocated" in codes
