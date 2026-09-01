from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any

from .audit import AuditLog
from .core import HostTools
from .tools import tool_definitions

SERVER_NAME = "host-sandbox"
SERVER_VERSION = "0.1.0"
MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"


class MCPServer:
    def __init__(self, tools: HostTools, audit: AuditLog) -> None:
        self.tools = tools
        self.audit = audit
        self.definitions = {tool["name"]: tool for tool in tool_definitions()}

    @staticmethod
    def _meta() -> dict[str, Any]:
        return {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}

    def _modern_result(self, payload: dict[str, Any], *, cache_scope: str = "private", ttl_ms: int = 0) -> dict[str, Any]:
        return {**payload, "resultType": "complete", "cacheScope": cache_scope, "ttlMs": ttl_ms, "_meta": self._meta()}

    def discover(self) -> dict[str, Any]:
        return self._modern_result(
            {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": (
                    "This server runs directly on the user's host OS with the permissions of the account that launched it. "
                    "Use file tools for transfers and edits, exec_command/exec_commands for host operations, and jobs for long tasks. "
                    "There is no container or filesystem sandbox boundary."
                ),
            },
            cache_scope="public",
            ttl_ms=60_000,
        )

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.definitions:
            raise KeyError(f"unknown tool: {name}")
        fn = getattr(self.tools, name, None)
        if fn is None:
            raise KeyError(f"tool is not implemented: {name}")
        started = time.time()
        # Avoid putting entire transferred payloads into the audit log.
        summary_args = dict(arguments)
        if "data_base64" in summary_args:
            summary_args["data_base64"] = f"<{len(str(summary_args['data_base64']))} base64 chars>"
        if "content" in summary_args and len(str(summary_args["content"])) > 400:
            summary_args["content"] = str(summary_args["content"])[:400] + "…"
        self.audit.add("tool", name, status="running", arguments=summary_args)
        try:
            result = fn(**arguments)
        except Exception as exc:
            self.audit.add("tool_result", name, status="error", duration_ms=round((time.time() - started) * 1000, 2), error=str(exc))
            raise
        self.audit.add("tool_result", name, status="ok", duration_ms=round((time.time() - started) * 1000, 2), result_preview=self._preview(result))
        return result

    @staticmethod
    def _preview(value: Any, max_chars: int = 900) -> str:
        def scrub(obj: Any) -> Any:
            if isinstance(obj, dict):
                out = {}
                for key, item in obj.items():
                    if key in {"content_base64", "data_base64", "blob", "data"} and isinstance(item, str) and len(item) > 512:
                        out[key] = f"<{len(item)} encoded chars>"
                    else:
                        out[key] = scrub(item)
                return out
            if isinstance(obj, list):
                return [scrub(item) for item in obj]
            return obj
        try:
            text = json.dumps(scrub(value), ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
        return text if len(text) <= max_chars else text[:max_chars] + "…"

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str):
            return self.error(request_id, -32600, "Invalid Request")

        # Notifications do not receive JSON-RPC responses.
        if request_id is None and method.startswith("notifications/"):
            self.audit.add("mcp", method)
            return None

        try:
            if method == "server/discover":
                result = self.discover()
            elif method == "initialize":
                requested = str(params.get("protocolVersion") or LEGACY_VERSION)
                negotiated = LEGACY_VERSION if requested != MODERN_VERSION else MODERN_VERSION
                result = {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Unrestricted host-native filesystem and command tools.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._modern_result({"tools": list(self.definitions.values())}, cache_scope="public", ttl_ms=60_000)
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return self.error(request_id, -32602, "Invalid tool call parameters")
                try:
                    payload = self._call_tool(name, arguments)
                    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
                    result = self._modern_result({"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": False})
                except Exception as exc:
                    text = f"{type(exc).__name__}: {exc}"
                    result = self._modern_result({"content": [{"type": "text", "text": text}], "isError": True})
            else:
                return self.error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            self.audit.add("mcp", method, status="error", error=str(exc), traceback=traceback.format_exc(limit=4))
            return self.error(request_id, -32603, f"Internal error: {exc}")

    @staticmethod
    def error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def run_stdio(server: MCPServer) -> int:
    server.audit.add("server", "MCP stdio server started", transport="stdio")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object")
            response = server.handle(request)
        except Exception as exc:
            response = MCPServer.error(None, -32700, f"Parse error: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0
