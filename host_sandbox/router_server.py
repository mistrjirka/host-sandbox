from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .http_security import HTTPRequestError, read_json_body, request_origin_allowed, require_token_for_network_bind
from .ssh_router import SSHRouter
from .router_mcp import RouterMCP


class RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr: tuple[str, int], router: SSHRouter, token: str | None, state_path: str | None = None):
        require_token_for_network_bind(addr[0], token)
        super().__init__(addr, RouterHandler)
        self.router = router
        self.aggregate_mcp = RouterMCP(router, state_path)
        self.auth_token = token


class RouterHandler(BaseHTTPRequestHandler):
    server: RouterHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:
        pass

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else self.headers.get("X-Host-Sandbox-Token", "")
        return secrets.compare_digest(supplied, token)

    def _json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
        self._json({"error": "unauthorized"}, 401)
        return False

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/health":
            if self.server.auth_token and not self._authorized():
                return self._json({"ok": True})
            try:
                return self._json({"ok": True, **self.server.router.list_hosts()})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 503)
        if path == "/hosts":
            if not self._require_auth():
                return
            return self._json(self.server.router.list_hosts())
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path != "/mcp" and not path.startswith("/mcp/"):
            self.close_connection = True
            return self._json({"error": "not found"}, 404)
        if not self._require_auth():
            return
        try:
            req = read_json_body(self)
            if not isinstance(req, dict):
                raise ValueError("JSON-RPC request must be an object")
            if path == "/mcp":
                response = self.server.aggregate_mcp.handle(req, self.headers.get("MCP-Protocol-Version"))
                if response is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._json(response)
            host = unquote(path[len("/mcp/"):])
            method = req.get("method")
            params = req.get("params") or {}
            result = self.server.router.client(host).request(str(method), params)
            return self._json({"jsonrpc": "2.0", "id": req.get("id"), "result": result})
        except HTTPRequestError as exc:
            self.close_connection = True
            return self._json({"error": str(exc)}, exc.status)
        except KeyError as exc:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.close_connection = True
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(exc)}}, 400)
        except Exception as exc:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}, 502)


def run_router_http(server: RouterHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.router.close()
        server.server_close()
