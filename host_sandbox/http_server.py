from __future__ import annotations

import json
import secrets
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audit import AuditLog
from .core import HostTools
from .dashboard import dashboard_html
from .http_security import HTTPRequestError, read_json_body, request_origin_allowed, require_token_for_network_bind
from .mcp import MCPServer


class HostHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        mcp: MCPServer,
        tools: HostTools,
        audit: AuditLog,
        host_name: str,
        token: str | None = None,
        *,
        mcp_enabled: bool = True,
    ):
        require_token_for_network_bind(addr[0], token)
        super().__init__(addr, HostHandler)
        self.mcp_server = mcp
        self.host_tools = tools
        self.audit_log = audit
        self.host_name = host_name
        self.auth_token = token
        self.mcp_enabled = mcp_enabled


class HostHandler(BaseHTTPRequestHandler):
    server: HostHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # The audit UI is the useful log; keep stderr quiet.
        return

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else self.headers.get("X-Host-Sandbox-Token", "")
        return secrets.compare_digest(supplied, token)

    def _json(self, value: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if headers:
                for k, v in headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _html(self, value: str) -> None:
        data = value.encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _guard(self) -> bool:
        if request_origin_allowed(self.headers, self.server.server_address[0]):
            return True
        self.close_connection = True
        self._json({"error": "untrusted Host or Origin"}, 403)
        return False

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.close_connection = True
        self._json({"error": "unauthorized"}, 401, {"WWW-Authenticate": "Bearer"})
        return False

    def _not_found(self) -> None:
        self.close_connection = True
        self._json({"error": "not found"}, 404)

    def do_OPTIONS(self) -> None:
        if not self._guard():
            return
        if urlparse(self.path).path != "/mcp" or not self.server.mcp_enabled:
            return self._not_found()
        self.send_response(204)
        self.send_header("Allow", "POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, MCP-Protocol-Version, MCP-Session-Id")
        self.send_header("Access-Control-Allow-Methods", "POST,DELETE,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path == "/":
            return self._html(dashboard_html(self.server.host_name, requires_auth=bool(self.server.auth_token)))
        if u.path == "/health":
            if self.server.auth_token and not self._authorized():
                return self._json({"ok": True})
            return self._json({"ok": True, "host": self.server.host_name, "host_mode": True})
        if u.path.startswith("/api/") and not self._require_auth():
            return
        if u.path == "/api/status":
            jobs = self.server.host_tools.list_jobs(1000)["jobs"]
            return self._json({"host_name": self.server.host_name, "host_mode": True, "sandboxed": False, "tool_calls": self.server.audit_log.tool_calls, "errors": self.server.audit_log.errors, "running_jobs": sum(j["status"] == "running" for j in jobs), "uptime_seconds": time.time() - self.server.audit_log.started_at, "latest_event_id": self.server.audit_log.latest_id})
        if u.path == "/api/events":
            try:
                after = int((parse_qs(u.query).get("after") or [0])[0])
            except ValueError:
                return self._json({"error": "invalid event cursor"}, 400)
            return self._json({"events": self.server.audit_log.after(after, 500)})
        if u.path == "/api/jobs":
            return self._json(self.server.host_tools.list_jobs(100))
        if u.path == "/mcp":
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._not_found()

    def do_DELETE(self) -> None:
        if not self._guard():
            return
        if urlparse(self.path).path != "/mcp" or not self.server.mcp_enabled:
            return self._not_found()
        if not self._require_auth():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if not self._guard():
            return
        if urlparse(self.path).path != "/mcp" or not self.server.mcp_enabled:
            return self._not_found()
        if not self._require_auth():
            return
        try:
            request = read_json_body(self)
        except HTTPRequestError as exc:
            self.close_connection = True
            return self._json({"error": str(exc)}, exc.status)
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            self.close_connection = True
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}, 400)
        if isinstance(request, list):
            responses = [r for item in request if isinstance(item, dict) for r in [self.server.mcp_server.handle(item)] if r is not None]
            return self._json(responses)
        if not isinstance(request, dict):
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, 400)
        response = self.server.mcp_server.handle(request)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        params = request.get("params")
        requested_version = params.get("protocolVersion") if isinstance(params, dict) else None
        return self._json(response, headers={"MCP-Protocol-Version": str(requested_version or self.headers.get("MCP-Protocol-Version") or "2025-06-18")})


def run_http(server: HostHTTPServer) -> None:
    server.audit_log.add("server", "HTTP/MCP server started", status="ok", address=f"http://{server.server_address[0]}:{server.server_address[1]}", mcp="/mcp" if server.mcp_enabled else None, dashboard="/")
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.server_close()
