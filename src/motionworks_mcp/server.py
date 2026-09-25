"""JSON-RPC 2.0 over stdio: the MCP transport.

Framing is one JSON object per line, which the MCP stdio transport permits and
which avoids the ``Content-Length`` header parsing that would earn nothing here.

No third-party dependency, deliberately: the server must start on any machine
that has a Python, with no resolution step between the user and a working tool.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, IO

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "motionworks-mcp"
SERVER_TITLE = "MotionWorks IEC Projects"

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class StdioServer:
    """Serves MCP requests from a line-delimited JSON stream."""

    def __init__(
        self,
        tools: dict[str, tuple[Callable[[dict[str, Any]], Any], dict[str, Any]]],
        title: str = SERVER_TITLE,
        version: str = "0.1.0",
        disabled: dict[str, str] | None = None,
    ) -> None:
        self.tools = tools
        self.disabled = disabled or {}
        self.title = title
        self.version = version

    # -- request handling -------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one request. Returns ``None`` for a notification."""
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return self._ok(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "title": self.title, "version": self.version},
                },
            )
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": self._tool_descriptors()})
        if method == "tools/call":
            return self._call(request_id, params)
        return self._error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    def _tool_descriptors(self) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for name, (_handler, schema) in sorted(self.tools.items()):
            descriptors.append(
                {
                    "name": name,
                    "description": schema.get("description", ""),
                    "inputSchema": {
                        key: value for key, value in schema.items() if key != "description"
                    },
                }
            )
        return descriptors

    def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, INVALID_PARAMS, "'arguments' must be an object")

        if name in self.disabled:
            from .envelope import disabled

            payload = disabled(name, "--enable-writes", self.disabled[name])
            return self._ok(request_id, _as_content(payload))

        entry = self.tools.get(str(name))
        if entry is None:
            available = ", ".join(sorted(self.tools))
            return self._error(
                request_id,
                METHOD_NOT_FOUND,
                f"unknown tool '{name}'. Available: {available}",
            )
        handler, _schema = entry
        try:
            outcome = handler(arguments)
        except Exception as exc:  # noqa: BLE001 - reported as a tool error, never a crash
            detail = f"{type(exc).__name__}: {exc}"
            payload = {
                "data": None,
                "findings": [{"code": "tool_error", "severity": "error", "message": detail}],
                "warnings": [],
                "normalisations": [],
                "ok": False,
                "meta": {"traceback": traceback.format_exc(limit=3)},
            }
            return self._ok(request_id, _as_content(payload), is_error=True)

        if hasattr(outcome, "as_dict"):
            payload_dict = outcome.as_dict()
        elif isinstance(outcome, dict):
            payload_dict = outcome
        else:
            payload_dict = {"data": outcome}
        is_error = payload_dict.get("ok") is False
        return self._ok(request_id, _as_content(payload_dict), is_error=is_error)

    @staticmethod
    def _ok(request_id: Any, result: Any, is_error: bool = False) -> dict[str, Any] | None:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any] | None:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # -- loop -------------------------------------------------------------

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
        """Read requests until the stream ends. Never raises."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(exc)}})
                continue
            if not isinstance(request, dict):
                _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "request must be an object"}})
                continue
            try:
                response = self.handle(request)
            except Exception as exc:  # noqa: BLE001 - the loop outlives any single request
                response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"},
                }
            if response is not None:
                _write(stdout, response)
        return 0


def _as_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap an envelope as MCP tool content.

    The envelope travels as JSON text, so a client that ignores ``structuredContent``
    still receives the whole answer, including findings and warnings.
    """
    text = json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload}


def _write(stdout: IO[str], message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, default=str) + "\n")
    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: serve MCP over stdio.

    Import failures are reported on stdout as a JSON-RPC error rather than left as
    a traceback, so a client that starts the server always learns why it is
    unusable instead of seeing a silent death. Exit status stays 0 either way;
    with ``failOnStartupError: false`` the host logs the failure and retries, so a
    broken import must not be allowed to look like a crash loop.
    """
    argv = argv if argv is not None else sys.argv[1:]
    if "--version" in argv:
        print(f"{SERVER_NAME} {_package_version()}")
        return 0
    if "--selftest" in argv:
        return _selftest()
    try:
        from .tools import DISABLED_TOOLS, TOOLS
    except Exception as exc:  # noqa: BLE001 - reported to the client, not raised
        _write(
            sys.stdout,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"motionworks-mcp failed to import: {type(exc).__name__}: {exc}",
                },
            },
        )
        print(f"motionworks-mcp import failed: {exc}", file=sys.stderr)
        return 1
    StdioServer(TOOLS, version=_package_version(), disabled=DISABLED_TOOLS).serve()
    return 0


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("motionworks-mcp")
    except Exception:  # noqa: BLE001 - source tree rather than an install
        return "0.1.0+source"


def _selftest() -> int:
    """Exercise every tool against nothing, to prove the roster loads."""
    from .tools import TOOLS

    for name in sorted(TOOLS):
        handler, schema = TOOLS[name]
        assert callable(handler), name
        assert schema.get("type") == "object", name
    print(f"{SERVER_NAME} selftest ok: {len(TOOLS)} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
