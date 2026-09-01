from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .agent_transport import AgentRegistry


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], registry: AgentRegistry, token: str):
        super().__init__(addr, AgentHandler)
        self.registry = registry
        self.agent_token = token


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:
        pass

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        return bool(self.server.agent_token) and secrets.compare_digest(supplied, self.server.agent_token)

    def _json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            return self._json({"ok": True, "agents": self.server.registry.list_agents()})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/agent/register":
                return self._json(self.server.registry.register(str(body.get("name") or ""), str(body.get("instance_id") or ""), body.get("system_info") or {}))
            if path == "/agent/poll":
                call = self.server.registry.poll(str(body["agent_id"]), int(body.get("wait_seconds", 20)))
                if call is None:
                    self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
                return self._json(call)
            if path == "/agent/result":
                response = body.get("response")
                if not isinstance(response, dict):
                    raise ValueError("response must be an object")
                self.server.registry.submit_result(str(body["agent_id"]), str(body["call_id"]), response)
                return self._json({"ok": True})
            if path == "/agent/disconnect":
                self.server.registry.disconnect(str(body["agent_id"]))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except KeyError as exc:
            return self._json({"error": str(exc)}, 404)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)


def run_agent_http(server: AgentHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.server_close()
