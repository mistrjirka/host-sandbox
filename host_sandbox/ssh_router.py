from __future__ import annotations

import json
import socket
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
    wol_mac: str | None = None
    wol_broadcast: str = "255.255.255.255"
    wol_port: int = 9
    wake_timeout_seconds: int = 120


def _mac_bytes(raw: str) -> bytes:
    compact = "".join(ch for ch in raw if ch.isalnum()).lower()
    if len(compact) != 12 or any(ch not in "0123456789abcdef" for ch in compact):
        raise ValueError(f"invalid Wake-on-LAN MAC address: {raw!r}")
    return bytes.fromhex(compact)


def _ssh_online(host: HostConfig, timeout_seconds: int = 3) -> bool:
    if host.local:
        return True
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout_seconds))}", host.ssh, "printf ok"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=max(2, int(timeout_seconds) + 2),
        )
        return proc.returncode == 0 and proc.stdout == "ok"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _send_magic_packet(host: HostConfig) -> None:
    if not host.wol_mac:
        raise RuntimeError(f"host {host.name!r} has no wake_on_lan configuration")
    mac = _mac_bytes(host.wol_mac)
    packet = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (host.wol_broadcast, int(host.wol_port)))


def wake_host(host: HostConfig, wait_seconds: int | None = None) -> dict[str, Any]:
    if host.local:
        return {"host": host.name, "online": True, "local": True, "woke": False}
    if _ssh_online(host, 2):
        return {"host": host.name, "online": True, "woke": False, "already_online": True}
    if not host.wol_mac:
        return {"host": host.name, "online": False, "woke": False, "wake_capable": False}

    timeout = host.wake_timeout_seconds if wait_seconds is None else max(0, int(wait_seconds))
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    while True:
        _send_magic_packet(host)
        attempts += 1
        if timeout <= 0:
            return {"host": host.name, "online": False, "woke": True, "wake_packet_sent": True, "attempts": attempts}
        # Poll SSH for up to ~10 seconds before retransmitting the magic packet.
        for _ in range(5):
            if _ssh_online(host, 2):
                return {
                    "host": host.name, "online": True, "woke": True, "attempts": attempts,
                    "ready_after_seconds": round(time.monotonic() - started, 2),
                }
            if time.monotonic() >= deadline:
                raise TimeoutError(f"host {host.name!r} did not become reachable over SSH within {timeout}s after Wake-on-LAN")
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


class RemoteMCP:
    def __init__(self, host: HostConfig) -> None:
        self.host = host
        self._lock = threading.RLock()
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None

    def _start(self) -> subprocess.Popen[str]:
        if self._proc and self._proc.poll() is None:
            return self._proc
        if not self.host.local and self.host.wol_mac:
            wake_host(self.host)
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
            wol = item.get("wake_on_lan") or {}
            if not isinstance(wol, dict):
                raise ValueError(f"host {item.get('name')!r} wake_on_lan must be an object")
            h = HostConfig(
                name=str(item["name"]),
                ssh=str(item.get("ssh") or ""),
                command=str(item.get("command") or "host-sandbox stdio"),
                local=bool(item.get("local", False)),
                wol_mac=str(wol["mac"]) if wol.get("mac") else None,
                wol_broadcast=str(wol.get("broadcast") or "255.255.255.255"),
                wol_port=int(wol.get("port") or 9),
                wake_timeout_seconds=int(wol.get("timeout_seconds") or 120),
            )
            if h.wol_mac:
                _mac_bytes(h.wol_mac)
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
            values.append({
                "name": h.name, "ssh": h.ssh, "local": False,
                "online": _ssh_online(h, 3), "wake_capable": bool(h.wol_mac),
            })
        return {"hosts": values}

    def wake_host(self, host: str, wait_seconds: int | None = None) -> dict[str, Any]:
        if host not in self.hosts:
            raise KeyError(f"unknown host: {host}")
        return wake_host(self.hosts[host], wait_seconds)

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
