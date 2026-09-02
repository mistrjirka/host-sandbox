from __future__ import annotations

import concurrent.futures
import json
import platform
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .core import HostTools
from .http_server import HostHTTPServer, run_http
from .mcp import MCPServer


def _http_json(url: str, token: str, body: dict[str, Any], *, timeout: float = 30) -> tuple[int, dict[str, Any] | None]:
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace")[:4000]
        raise RuntimeError(f"hub returned HTTP {exc.code}: {detail}") from exc


def _post_empty_ok(url: str, token: str, body: dict[str, Any], *, timeout: float = 10) -> None:
    _http_json(url, token, body, timeout=timeout)


def _payload_expired(payload: dict[str, Any], *, now: float | None = None) -> bool:
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return float(expires_at) <= (time.time() if now is None else now)


def run_connected_agent(
    *,
    hub: str,
    token: str,
    name: str,
    cwd: str,
    state_dir: Path,
    dashboard_bind: str = "127.0.0.1",
    dashboard_port: int = 8765,
    dashboard: bool = True,
) -> int:
    if not token:
        raise ValueError("agent token is required")
    hub = hub.rstrip("/")
    audit = AuditLog(state_dir)
    tools = HostTools(audit, state_dir, cwd)
    mcp = MCPServer(tools, audit)
    original_system_info = tools.system_info
    def system_info() -> dict[str, Any]:
        value = original_system_info(); value.update({"host_name": name, "connection_mode": "foreground-agent"}); return value
    tools.system_info = system_info  # type: ignore[method-assign]

    instance_id = uuid.uuid4().hex
    status, registration = _http_json(hub + "/agent/register", token, {
        "name": name,
        "instance_id": instance_id,
        "system_info": system_info(),
    })
    if status != 200 or not registration:
        raise RuntimeError("hub registration failed")
    agent_id = str(registration["agent_id"])
    stop = threading.Event()
    dashboard_server: HostHTTPServer | None = None
    dashboard_thread: threading.Thread | None = None

    if dashboard:
        dashboard_server = HostHTTPServer((dashboard_bind, dashboard_port), mcp, tools, audit, name, None)
        dashboard_thread = threading.Thread(target=run_http, args=(dashboard_server,), name="host-sandbox-dashboard", daemon=True)
        dashboard_thread.start()

    def request_stop(*_: object) -> None:
        stop.set()
    old_int = signal.signal(signal.SIGINT, request_stop)
    old_term = signal.signal(signal.SIGTERM, request_stop)
    audit.add("server", "Connected to Host Sandbox hub", status="ok", hub=hub, host_name=name, agent_id=agent_id)
    print(f"Host Sandbox [{name}] connected to {hub}", flush=True)
    print("Control is available only while this process is running.", flush=True)
    if dashboard:
        print(f"Dashboard: http://{dashboard_bind}:{dashboard_port}/", flush=True)
    print("Press Ctrl-C to disconnect.", flush=True)

    def handle_call(payload: dict[str, Any], current_agent_id: str) -> None:
        call_id = str(payload["call_id"])
        if _payload_expired(payload):
            audit.add("connection", "Dropped expired tool call", status="error", call_id=call_id)
            return
        request = {"jsonrpc": "2.0", "id": call_id, "method": payload["method"], "params": payload.get("params") or {}}
        response = mcp.handle(request)
        if response is None:
            response = {"jsonrpc": "2.0", "id": call_id, "result": {}}
        try:
            _post_empty_ok(hub + "/agent/result", token, {"agent_id": current_agent_id, "call_id": call_id, "response": response}, timeout=30)
        except Exception as exc:
            audit.add("connection", "Failed to return tool result", status="error", call_id=call_id, error=str(exc))

    max_inflight_calls = 16
    call_slots = threading.BoundedSemaphore(max_inflight_calls)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_inflight_calls, thread_name_prefix="host-sandbox-call")

    def run_call(payload: dict[str, Any], current_agent_id: str) -> None:
        try:
            handle_call(payload, current_agent_id)
        finally:
            call_slots.release()

    try:
        while not stop.is_set():
            if not call_slots.acquire(timeout=0.5):
                continue
            slot_owned = True
            try:
                status, payload = _http_json(hub + "/agent/poll", token, {"agent_id": agent_id, "wait_seconds": 2}, timeout=6)
                if status == 204 or not payload:
                    call_slots.release()
                    slot_owned = False
                    continue
                pool.submit(run_call, payload, agent_id)
                slot_owned = False
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                if slot_owned:
                    call_slots.release()
                    slot_owned = False
                if stop.is_set():
                    break
                audit.add("connection", "Hub connection error", status="error", error=str(exc))
                time.sleep(2)
                # Re-register after a hub restart or expired agent id.
                try:
                    _, registration = _http_json(hub + "/agent/register", token, {"name": name, "instance_id": instance_id, "system_info": system_info()}, timeout=10)
                    if registration:
                        agent_id = str(registration["agent_id"])
                except Exception:
                    pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        try:
            _post_empty_ok(hub + "/agent/disconnect", token, {"agent_id": agent_id}, timeout=3)
        except Exception:
            pass
        if dashboard_server is not None:
            dashboard_server.shutdown(); dashboard_server.server_close()
        tools.close()
        signal.signal(signal.SIGINT, old_int); signal.signal(signal.SIGTERM, old_term)
        print(f"Host Sandbox [{name}] disconnected", flush=True)
    return 0
