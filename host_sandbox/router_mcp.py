from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .ssh_router import SSHRouter


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _session_schema(extra: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    props = {"session_id": {"type": "string", "pattern": "^s_[a-f0-9]{16}$"}}
    props.update(extra or {})
    return _obj(props, ["session_id", *(required or [])])


ROUTER_TOOLS: list[dict[str, Any]] = [
    {"name": "sandbox_health", "description": "Check that the Host Sandbox router and configured computers are reachable.", "inputSchema": _obj({})},
    {"name": "list_projects", "description": "List configured computers. Each computer is exposed as a Development-Sandbox-style project.", "inputSchema": _obj({})},
    {"name": "list_sessions", "description": "List logical host sessions. A session selects one computer but does not create a container.", "inputSchema": _obj({"active_only": {"type": "boolean", "default": False}})},
    {"name": "create_session", "description": "Create a logical session for a configured computer. No container is created; tools run directly on that host OS.", "inputSchema": _obj({"project": {"type": "string"}, "label": {"type": ["string", "null"], "maxLength": 100}}, ["project"])},
    {"name": "destroy_session", "description": "Remove a logical host session. Files and processes on the computer are not deleted.", "inputSchema": _obj({"session_id": {"type": "string"}}, ["session_id"])},
    {"name": "path_info", "description": "Inspect a file or directory on the selected computer.", "inputSchema": _session_schema({"repo": {"type": "string", "default": "."}, "path": {"type": "string", "default": "."}, "include_hidden": {"type": "boolean", "default": True}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 2000}})},
    {"name": "list_repositories", "description": "Discover Git repositories below the selected computer's workspace root.", "inputSchema": _session_schema({"max_depth": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3}, "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000}})},
    {"name": "exec_command", "description": "Run an unrestricted command directly on the selected computer.", "inputSchema": _session_schema({"command": {"type": "string", "maxLength": 1000000}, "cwd": {"type": "string", "default": "."}, "env": {"type": ["object", "null"], "additionalProperties": {"type": "string"}}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 3600}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20, "default": 8}, "max_output_bytes": {"type": "integer", "minimum": 1000, "maximum": 2097152, "default": 131072}}, ["command"])},
    {"name": "exec_commands", "description": "Run independent commands on one or more selected computers concurrently.", "inputSchema": _obj({"commands": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "properties": {"session_id": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string", "default": "."}, "env": {"type": ["object", "null"], "additionalProperties": {"type": "string"}}, "timeout_seconds": {"type": "integer", "default": 3600}, "wait_seconds": {"type": "integer", "default": 8}, "max_output_bytes": {"type": "integer", "default": 131072}}, "required": ["session_id", "command"], "additionalProperties": False}}, "concurrency": {"type": "integer", "minimum": 1, "maximum": 32, "default": 16}}, ["commands"])},
    {"name": "list_jobs", "description": "List recent command jobs for a host session.", "inputSchema": _session_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 50}})},
    {"name": "get_job", "description": "Read a host command job and its paginated output.", "inputSchema": _session_schema({"job_id": {"type": "string"}, "stdout_offset": {"type": "integer", "minimum": 0, "default": 0}, "stderr_offset": {"type": "integer", "minimum": 0, "default": 0}, "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 2097152, "default": 131072}}, ["job_id"])},
    {"name": "terminate_job", "description": "Stop a running command job on a computer.", "inputSchema": _session_schema({"job_id": {"type": "string"}, "signal": {"type": "string", "enum": ["TERM", "INT", "KILL", "HUP"], "default": "TERM"}, "force_after_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 5}}, ["job_id"])},
    {"name": "search_project", "description": "Search text below a repository/path on a selected computer.", "inputSchema": _session_schema({"query": {"type": "string"}, "repo": {"type": "string", "default": "."}, "path": {"type": "string", "default": "."}, "glob": {"type": ["array", "null"], "items": {"type": "string"}}, "fixed_strings": {"type": "boolean", "default": False}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500}}, ["query"])},
    {"name": "read_file", "description": "Read a line-numbered text chunk from a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "repo": {"type": "string", "default": "."}, "start_line": {"type": "integer", "minimum": 1, "default": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 1000}}, ["path"])},
    {"name": "write_file", "description": "Create or replace a text file on a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "content": {"type": "string"}, "repo": {"type": "string", "default": "."}, "create_parents": {"type": "boolean", "default": True}, "overwrite": {"type": "boolean", "default": True}}, ["path", "content"])},
    {"name": "replace_text", "description": "Replace exact text in a file on a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "repo": {"type": "string", "default": "."}, "expected_occurrences": {"type": ["integer", "null"]}}, ["path", "old_text", "new_text"])},
    {"name": "read_binary_file", "description": "Read a binary file from a selected computer as base64 transfer data.", "inputSchema": _session_schema({"path": {"type": "string"}, "repo": {"type": "string", "default": "."}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16777216, "default": 16777216}}, ["path"])},
    {"name": "write_binary_file", "description": "Write raw bytes supplied as base64 to a selected computer.", "inputSchema": _session_schema({"path": {"type": "string"}, "content_base64": {"type": "string"}, "repo": {"type": "string", "default": "."}, "create_parents": {"type": "boolean", "default": True}, "overwrite": {"type": "boolean", "default": True}}, ["path", "content_base64"])},
    {"name": "list_processes", "description": "List processes on the selected computer.", "inputSchema": _session_schema({"max_processes": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500}})},
    {"name": "signal_process", "description": "Signal a process on the selected computer.", "inputSchema": _session_schema({"pid": {"type": "integer", "minimum": 2}, "signal": {"type": "string", "enum": ["TERM", "INT", "KILL", "HUP", "CONT", "STOP"], "default": "TERM"}}, ["pid"])},
]


@dataclass
class LogicalSession:
    id: str
    project: str
    label: str | None
    created_at: float
    running: bool = True


class RouterMCP:
    """Aggregate multiple host-sandbox instances behind one MCP endpoint."""

    def __init__(self, router: SSHRouter, state_path: str | Path | None = None) -> None:
        self.router = router
        self._lock = threading.RLock()
        self._sessions: dict[str, LogicalSession] = {}
        self.state_path = Path(state_path).expanduser() if state_path else None
        self._load()

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text())
            for item in raw.get("sessions", []):
                s = LogicalSession(**item)
                self._sessions[s.id] = s
        except Exception:
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"sessions": [asdict(s) for s in self._sessions.values()]}, indent=2))
        tmp.replace(self.state_path)

    @staticmethod
    def _sid(project: str, created: float) -> str:
        return "s_" + hashlib.sha256(f"{project}:{created}".encode()).hexdigest()[:16]

    def _session(self, sid: str) -> LogicalSession:
        with self._lock:
            s = self._sessions.get(sid)
        if not s or not s.running:
            raise KeyError(f"unknown or inactive session: {sid}")
        if s.project not in self.router.hosts:
            raise KeyError(f"session host is no longer configured: {s.project}")
        return s

    @staticmethod
    def _payload(result: Any) -> Any:
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        return result

    def _remote(self, sid: str, tool: str, args: dict[str, Any]) -> Any:
        s = self._session(sid)
        return self._payload(self.router.call_tool(s.project, tool, args))

    @staticmethod
    def _join(repo: str = ".", path: str = ".") -> str:
        repo = repo or "."; path = path or "."
        if repo == ".": return path
        if path == ".": return repo
        return str(Path(repo) / path)

    def call(self, name: str, a: dict[str, Any]) -> Any:
        if name == "sandbox_health":
            return {"ok": True, "router": "host-sandbox", **self.router.list_hosts()}
        if name == "list_projects":
            return {"projects": [{"id": h.name, "name": h.name, "description": "Local host OS" if h.local else f"Host OS via {h.ssh}"} for h in self.router.hosts.values()]}
        if name == "list_sessions":
            active_only = bool(a.get("active_only", False))
            with self._lock:
                rows = [asdict(s) for s in self._sessions.values() if not active_only or s.running]
            return {"sessions": rows}
        if name == "create_session":
            project = str(a["project"])
            if project not in self.router.hosts: raise KeyError(f"unknown project/host: {project}")
            now = time.time(); s = LogicalSession(self._sid(project, now), project, a.get("label"), now)
            with self._lock: self._sessions[s.id] = s; self._save()
            return {"id": s.id, "project": project, "label": s.label, "running": True, "container": None, "host_mode": True}
        if name == "destroy_session":
            s = self._session(str(a["session_id"])); s.running = False
            with self._lock: self._save()
            return {"id": s.id, "project": s.project, "running": False}

        sid = str(a.pop("session_id"))
        if name == "exec_command":
            args = {k: a[k] for k in ("command","cwd","env","timeout_seconds","wait_seconds","max_output_bytes") if k in a}
            return self._remote(sid, "exec_command", args)
        if name == "list_jobs":
            return self._remote(sid, "list_jobs", {"limit": a.get("limit", 50)})
        if name == "get_job":
            # Current host agent has a single combined output stream. Expose it as stdout until
            # the host job backend reaches exact stdout/stderr parity.
            r = self._remote(sid, "read_job", {"job_id": a["job_id"], "offset": a.get("stdout_offset", 0), "max_bytes": a.get("max_bytes", 131072)})
            out = r.pop("output", "")
            r.update({"stdout": out, "stderr": "", "stdout_offset": a.get("stdout_offset", 0), "stdout_next_offset": r.pop("next_offset", 0), "stderr_offset": a.get("stderr_offset", 0), "stderr_next_offset": a.get("stderr_offset", 0)})
            return r
        if name == "terminate_job":
            return self._remote(sid, "signal_job", {"job_id": a["job_id"], "sig": a.get("signal", "TERM")})
        if name == "path_info":
            target = self._join(a.get("repo", "."), a.get("path", "."))
            info = self._remote(sid, "path_info", {"path": target})
            if info.get("kind") != "directory": return {**info, "type": info.pop("kind", info.get("type"))}
            listing = self._remote(sid, "list_dir", {"path": target, "max_entries": a.get("max_entries", 2000)})
            return {**info, "type": "directory", "entries": listing.get("entries", []), "entry_count_returned": len(listing.get("entries", [])), "truncated": listing.get("truncated", False)}
        if name == "list_repositories":
            cmd = "find . -mindepth 1 -maxdepth %d -type d -name .git -printf '%%h\\n' 2>/dev/null | head -n %d" % (a.get("max_depth",3)+1, a.get("limit",1000))
            r = self._remote(sid, "exec_command", {"command": cmd, "cwd": ".", "wait_seconds": 20, "max_output_bytes": 2097152})
            rows = [x for x in r.get("output_tail", "").splitlines() if x]
            return {"repositories": [{"path": x[2:] if x.startswith("./") else x, "name": Path(x).name} for x in rows], "truncated": len(rows) >= a.get("limit",1000)}
        if name == "search_project":
            target = self._join(a.get("repo", "."), a.get("path", "."))
            # Underlying search_text doesn't yet expose glob filtering.
            return self._remote(sid, "search_text", {"query": a["query"], "path": target, "max_results": a.get("max_results",500), "fixed_strings": a.get("fixed_strings",False)})
        if name == "read_file":
            target = self._join(a.get("repo", "."), a["path"])
            raw = self._remote(sid, "read_file", {"path": target, "offset": 0, "max_bytes": 1048576})
            lines = raw.get("text", "").splitlines()
            start = a.get("start_line",1); max_lines = a.get("max_lines",1000); end = min(len(lines), start-1+max_lines)
            content = "\n".join(f"{i+1}: {lines[i]}" for i in range(start-1,end))
            return {"path": target, "start_line": start, "end_line": end, "total_lines": len(lines), "has_more": end < len(lines), "eof": end >= len(lines), "next_start_line": end+1 if end < len(lines) else None, "content": content}
        if name == "write_file":
            target = self._join(a.get("repo", "."), a["path"])
            mode = "overwrite" if a.get("overwrite",True) else "exclusive"
            return self._remote(sid, "write_file", {"path": target, "content": a["content"], "mode": mode, "create_parents": a.get("create_parents",True)})
        if name == "replace_text":
            target = self._join(a.get("repo", "."), a["path"])
            return self._remote(sid, "replace_text", {"path": target, "old": a["old_text"], "new": a["new_text"], "expected_matches": a.get("expected_occurrences")})
        if name == "read_binary_file":
            target = self._join(a.get("repo", "."), a["path"])
            return self._remote(sid, "read_file_chunk", {"path": target, "offset": 0, "length": min(a.get("max_bytes",16777216),1048576)})
        if name == "write_binary_file":
            target = self._join(a.get("repo", "."), a["path"])
            if not a.get("overwrite",True):
                try: self._remote(sid, "path_info", {"path": target}); raise FileExistsError(target)
                except Exception: pass
            return self._remote(sid, "write_file_chunk", {"path": target, "data_base64": a["content_base64"], "offset": 0, "create_parents": a.get("create_parents",True), "truncate_after": True, "final_sha256": True})
        if name == "list_processes":
            return self._remote(sid, "list_processes", {"max_processes": a.get("max_processes",500)})
        if name == "signal_process":
            return self._remote(sid, "signal_process", {"pid": a["pid"], "sig": a.get("signal","TERM")})
        raise KeyError(f"unknown router tool: {name}")

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        rid = req.get("id"); method = req.get("method"); params = req.get("params") or {}
        if rid is None and str(method).startswith("notifications/"): return None
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "host-sandbox-router", "version": "0.2.0"}, "instructions": "Multiple computers exposed through Development-Sandbox-style logical sessions. Tools execute directly on host OSes."}
            elif method == "ping": result = {}
            elif method == "tools/list": result = {"tools": ROUTER_TOOLS}
            elif method == "tools/call":
                name = params.get("name"); arguments = dict(params.get("arguments") or {})
                payload = self.call(str(name), arguments)
                result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str, indent=2)}], "structuredContent": payload, "isError": False}
            else: raise KeyError(f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
