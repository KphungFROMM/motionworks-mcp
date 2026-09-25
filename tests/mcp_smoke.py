"""Drive the MCP server over stdio exactly as a host would.

Sends newline-delimited JSON-RPC and reports what comes back. This is the
end-to-end smoke test: if this passes, the host can talk to the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
CORPUS = r"C:\DeepseekAI\AryaAIDSH\_scratch\yaskawa-projects\Yaskawa"


def run(requests: list[dict], env_extra: dict[str, str] | None = None) -> list[dict]:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["PYTHONUTF8"] = "1"
    if env_extra:
        env.update(env_extra)
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "motionworks_mcp.server"],
        input=payload,
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=300,
    )
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"unparsable": line})
    return out, proc.stderr


def main() -> int:
    project = os.path.join(CORPUS, "MP2600iec Program", "TopCutter")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mw_ping", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "mw_list_projects", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "mw_describe_project", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "mw_list_pous", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "mw_get_pou_body", "arguments": {"project": project, "pou": "TopCutterCutControl"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "mw_get_pou_decls", "arguments": {"project": project, "pou": "TopCutterCutControl"}}},
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "mw_get_pou_logic", "arguments": {"project": project, "pou": "ServoTaskSlow"}}},
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "mw_get_globals", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "mw_get_tasks", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "mw_get_hardware", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "mw_get_library_deps", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "mw_get_data_types", "arguments": {"project": project}}},
        {"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "mw_search", "arguments": {"project": project, "query": "fbCamIn", "limit": 5}}},
        {"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "mw_validate_st", "arguments": {"project": project, "pou": "TopCutterCutControl"}}},
        {"jsonrpc": "2.0", "id": 17, "method": "tools/call", "params": {"name": "mw_pou_impact", "arguments": {"project": project, "target": "TopCutter"}}},
        {"jsonrpc": "2.0", "id": 18, "method": "tools/call", "params": {"name": "mw_write_st", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "mw_describe_project", "arguments": {"project": "C:\\does\\not\\exist"}}},
        {"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "mw_list_projects", "arguments": {}}},
    ]
    responses, stderr = run(requests, {"ARYAAI_MOTIONWORKS_ROOTS": CORPUS})
    by_id = {response.get("id"): response for response in responses}

    def envelope(response_id):
        response = by_id.get(response_id, {})
        result = response.get("result") or {}
        return result.get("structuredContent") or {}

    print(f"responses: {len(responses)}")
    tools = by_id.get(2, {}).get("result", {}).get("tools", [])
    print(f"tools/list -> {len(tools)} tools")
    print("   " + ", ".join(tool["name"] for tool in tools))
    print()

    def show(label, response_id, summarise):
        env = envelope(response_id)
        error = by_id.get(response_id, {}).get("error")
        if error:
            print(f"{label:<26} ERROR {error.get('code')}: {error.get('message')[:80]}")
            return
        if env.get("meta", {}).get("capability_disabled"):
            print(f"{label:<26} capability_disabled: {env['meta']['detail'][:60]}")
            return
        print(f"{label:<26} ok={env.get('ok')} warnings={len(env.get('warnings', []))} "
              f"findings={len(env.get('findings', []))} norm={len(env.get('normalisations', []))} {summarise(env)}")

    show("mw_ping", 3, lambda e: f"version={e['data']['version']}")
    show("mw_list_projects", 4, lambda e: f"projects={len(e['data'])}")
    show("mw_describe_project", 5, lambda e: f"{e['data']['name']} pous={e['data']['pou_count']} tasks={e['data']['task_count']}")
    show("mw_list_pous", 6, lambda e: f"pous={len(e['data'])}")
    show("mw_get_pou_body", 7, lambda e: f"kind={e['data']['body_kind']} chars={e['data']['chars']} lines={e['data']['lines']}")
    show("mw_get_pou_decls", 8, lambda e: f"vars={e['data']['variable_count']} grid={e['data']['grid_records']}/{e['data']['grid_declared_count']}")
    show("mw_get_pou_logic", 9, lambda e: f"networks={e['data']['network_count']} calls={e['data']['call_count']}")
    show("mw_get_globals", 10, lambda e: f"vars={e['data']['variable_count']} addresses={e['data']['address_count']} comments={len(e['data']['comments'])}")
    show("mw_get_tasks", 11, lambda e: f"tasks={len(e['data'])} names={[t.get('name') for t in e['data']]}")
    show("mw_get_hardware", 12, lambda e: f"target={e['data']['hardware'].get('firmware_model')} axes={len(e['data']['axes'])}")
    show("mw_get_library_deps", 13, lambda e: f"libs={len(e['data'])}")
    show("mw_get_data_types", 14, lambda e: f"types={e['data']['declaration_count']}")
    show("mw_search", 15, lambda e: f"hits={e['data']['hit_count']}")
    show("mw_validate_st", 16, lambda e: f"externals={e['meta'].get('declared_externals')} checker={e['meta'].get('external_checker', {}).get('status')}")
    show("mw_pou_impact", 17, lambda e: f"callers={e['data']['caller_count']}")
    show("mw_write_st", 18, lambda e: "unexpected")
    show("no_such_tool", 19, lambda e: "unexpected")
    show("mw_describe_project bad", 20, lambda e: "unexpected")
    show("mw_list_projects no env", 21, lambda e: f"projects={len(e['data'])}")

    print()
    if stderr.strip():
        print("stderr:", stderr.strip()[:400])
    print("validation detail:")
    env = envelope(16)
    for finding in env.get("findings", [])[:6]:
        print(f"   [{finding['severity']}] {finding['code']}: {finding['message'][:110]}")
    for norm in env.get("normalisations", [])[:4]:
        print(f"   [norm] {norm['code']}: {norm['message'][:110]}")
    checker = env.get("meta", {}).get("external_checker", {})
    if checker.get("problems"):
        print(f"   checker problems: {len(checker['problems'])}")
        for problem in checker["problems"][:3]:
            print(f"      {problem['code']}: {problem['message'][:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
