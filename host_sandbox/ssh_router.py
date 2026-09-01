from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HostConfig:
    name: str
    ssh: str = ""
    command: str = "host-sandbox stdio"
    local: bool = False


class RemoteMCP:
    def __init__(self, host: HostConfig) -> None:
        self.host = host
        self._lock = threading.RLock()
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None

    def _start(self) -> subprocess.Popen[str]:
        if self._proc and self._proc.poll() is None:
            return self._proc
        argv = ["/bin/bash", "-lc", self.host.command] if self.host.local else ["ssh", "-T", self.host.ssh, self.host.command]
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self._proc

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            p = self._start()
            assert p.stdin is not None and p.stdout is not None
            rid = self._next_id
            self._next_id += 1
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
            if not line:
                err = p.stderr.read() if p.stderr else ""
                raise RuntimeError(f"SSH MCP session to {self.host.name} closed: {err.strip()}")
            msg = json.loads(line)
            if msg.get("id") != rid:
                raise RuntimeError(f"unexpected MCP response id from {self.host.name}")
            if "error" in msg:
                raise RuntimeError(str(msg["error"]))
            return msg.get("result")

    def close(self) -> None:
        with self._lock:
            p = self._proc
            if p is None:
                return
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=2)
            for stream in (p.stdin, p.stdout, p.stderr):
                if stream is not None:
                    try: stream.close()
                    except OSError: pass
            self._proc = None


class SSHRouter:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path).expanduser()
        self.hosts: dict[str, HostConfig] = {}
        self.clients: dict[str, RemoteMCP] = {}
        self.reload()

    def reload(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        hosts = raw.get("hosts") or []
        parsed: dict[str, HostConfig] = {}
        for item in hosts:
            h = HostConfig(name=str(item["name"]), ssh=str(item.get("ssh") or ""), command=str(item.get("command") or "host-sandbox stdio"), local=bool(item.get("local", False)))
            if not h.local and not h.ssh:
                raise ValueError(f"host {h.name!r} requires ssh or local=true")
            parsed[h.name] = h
        self.hosts = parsed
        for name in list(self.clients):
            if name not in self.hosts:
                self.clients.pop(name).close()

    def list_hosts(self) -> dict[str, Any]:
        values = []
        for h in self.hosts.values():
            if h.local:
                values.append({"name": h.name, "local": True, "online": True})
                continue
            proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", h.ssh, "printf ok"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
            values.append({"name": h.name, "ssh": h.ssh, "local": False, "online": proc.returncode == 0 and proc.stdout == "ok"})
        return {"hosts": values}

    def client(self, host: str) -> RemoteMCP:
        if host not in self.hosts:
            raise KeyError(f"unknown host: {host}")
        c = self.clients.get(host)
        if c is None:
            c = RemoteMCP(self.hosts[host])
            self.clients[host] = c
        return c

    def call_tool(self, host: str, name: str, arguments: dict[str, Any]) -> Any:
        return self.client(host).request("tools/call", {"name": name, "arguments": arguments})

    def tools(self, host: str) -> Any:
        return self.client(host).request("tools/list", {})

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
