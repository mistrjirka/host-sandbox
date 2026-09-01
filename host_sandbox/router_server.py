from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .ssh_router import SSHRouter


class RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], router: SSHRouter, token: str | None):
        super().__init__(addr, RouterHandler)
        self.router = router
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            try:
                return self._json({"ok": True, **self.server.router.list_hosts()})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 503)
        if path == "/hosts":
            if not self._authorized():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(self.server.router.list_hosts())
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/mcp/"):
            return self._json({"error": "not found"}, 404)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        host = unquote(path[len("/mcp/"):])
        if not host:
            return self._json({"error": "missing host"}, 400)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("JSON-RPC request must be an object")
            method = req.get("method")
            params = req.get("params") or {}
            result = self.server.router.client(host).request(str(method), params)
            return self._json({"jsonrpc": "2.0", "id": req.get("id"), "result": result})
        except KeyError as exc:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}}, 404)
        except Exception as exc:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}, 502)


def run_router_http(server: RouterHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.router.close()
        server.server_close()
